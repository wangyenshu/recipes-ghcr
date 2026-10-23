"""Serve a ghcr.io (OCI) conda channel as a plain HTTP channel on localhost.

rattler-build can't read ``oci://`` channels, so builds point it at this relay
instead: ``http://127.0.0.1:8123/<channel>`` serves

* ``<channel>/<subdir>/repodata.json``  from ``ghcr.io/<namespace>/<channel>/<subdir>/repodata.json:latest``
* ``<channel>/<subdir>/<package file>`` from that package's blob on ghcr.io

using the same layout the upload step publishes (see ghcr.py). Anything else
(``repodata.json.zst``, sharded indexes, ...) answers 404, so clients fall back to
``repodata.json``. Only the standard library is used, so it runs in any environment.

    python -m emci.ghcr_bridge serve            # until Ctrl-C
    python -m emci.ghcr_bridge run -- CMD ...   # serve while CMD runs

Settings (environment): GHCR_BRIDGE_REGISTRY (default https://ghcr.io),
GHCR_BRIDGE_NAMESPACE (default: the repository owner, else wangyenshu),
GHCR_BRIDGE_TOKEN (optional, only needed for private packages).
"""

import argparse
import base64
import contextlib
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULT_PORT = 8123
REPODATA_MEDIA_TYPE = "application/vnd.conda.repodata.v1+json"
INDEX_TTL = 300          # seconds an index is reused for package lookups
TOKEN_TTL = 240          # registry tokens last at least 5 minutes


def default_namespace():
    owner = os.environ.get("GHCR_BRIDGE_NAMESPACE") or os.environ.get("GITHUB_REPOSITORY_OWNER") or "wangyenshu"
    return owner.lower()


def oci_name(name):
    return f"zzz{name}" if name.startswith("_") else name


def package_name(filename):
    for ext in (".tar.bz2", ".conda"):
        if filename.endswith(ext):
            return filename[: -len(ext)].rsplit("-", 2)[0]
    return None


class NotFound(Exception):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class OciChannels:
    """Read access to the conda channels under ghcr.io/<namespace>/."""

    def __init__(self, registry=None, namespace=None, token=None):
        self.registry = (registry or os.environ.get("GHCR_BRIDGE_REGISTRY") or "https://ghcr.io").rstrip("/")
        self.namespace = namespace or default_namespace()
        self.token = token if token is not None else os.environ.get("GHCR_BRIDGE_TOKEN")
        self.opener = urllib.request.build_opener(_NoRedirect)
        self._tokens = {}
        self._indexes = {}
        self._lock = threading.Lock()

    # -- HTTP ----------------------------------------------------------------

    def _open(self, url, headers=None):
        """GET `url`; returns the open response. Raises NotFound or OSError."""
        for attempt in range(1, 5):
            try:
                return self.opener.open(urllib.request.Request(url, headers=headers or {}), timeout=120)
            except urllib.error.HTTPError as e:
                if e.code in (301, 302, 303, 307, 308):
                    return e          # caller follows the redirect itself (without credentials)
                if e.code in (401, 403, 404):
                    # ghcr answers "denied" for repositories that don't exist
                    raise NotFound(f"{url}: HTTP {e.code}")
                if e.code < 500 and e.code != 429 or attempt == 4:
                    raise OSError(f"{url}: HTTP {e.code}")
            except urllib.error.URLError as e:
                if attempt == 4:
                    raise OSError(f"{url}: {e.reason}")
            time.sleep(2 ** attempt)

    def _registry_token(self, repo):
        with self._lock:
            token, expires = self._tokens.get(repo, (None, 0))
        if token and time.time() < expires:
            return token
        headers = {}
        if self.token:
            basic = base64.b64encode(f"token:{self.token}".encode()).decode()
            headers["Authorization"] = f"Basic {basic}"
        query = urllib.parse.urlencode({"scope": f"repository:{repo}:pull", "service": urllib.parse.urlparse(self.registry).hostname})
        with self._open(f"{self.registry}/token?{query}", headers) as response:
            token = json.load(response)["token"]
        with self._lock:
            self._tokens[repo] = (token, time.time() + TOKEN_TTL)
        return token

    def _get(self, repo, path, accept=None):
        """GET /v2/<repo>/<path> on the registry, following blob redirects."""
        headers = {"Authorization": f"Bearer {self._registry_token(repo)}"}
        if accept:
            headers["Accept"] = accept
        response = self._open(f"{self.registry}/v2/{repo}/{path}", headers)
        if getattr(response, "code", 200) in (301, 302, 303, 307, 308):
            location = response.headers["Location"]
            response.close()
            response = self._open(urllib.parse.urljoin(f"{self.registry}/", location))  # signed URL, no credentials
        return response

    # -- channel content -------------------------------------------------------

    def repodata(self, channel, subdir):
        """The subdir's repodata.json as (bytes, digest). Raises NotFound."""
        repo = f"{self.namespace}/{channel}/{subdir}/repodata.json"
        with self._get(repo, "manifests/latest", accept="application/vnd.oci.image.manifest.v1+json") as response:
            manifest = json.load(response)
        layers = [l for l in manifest.get("layers", []) if l.get("mediaType") == REPODATA_MEDIA_TYPE]
        if not layers:
            raise NotFound(f"{repo}: no {REPODATA_MEDIA_TYPE} layer")
        digest = layers[0]["digest"]
        with self._get(repo, f"blobs/{digest}") as response:
            data = response.read()
        index = json.loads(data)
        with self._lock:
            self._indexes[(channel, subdir)] = (index, time.time())
        return data, digest

    def _record(self, channel, subdir, filename):
        for attempt in (1, 2):
            with self._lock:
                index, fetched = self._indexes.get((channel, subdir), (None, 0))
            if index is None or attempt == 2 or time.time() - fetched > INDEX_TTL:
                self.repodata(channel, subdir)
                with self._lock:
                    index, _ = self._indexes[(channel, subdir)]
            for key in ("packages", "packages.conda"):
                if filename in index.get(key, {}):
                    return index[key][filename]
        raise NotFound(f"{channel}/{subdir}/{filename} is not in the index")

    def package(self, channel, subdir, filename):
        """An open response streaming the package file. Raises NotFound."""
        name = package_name(filename)
        if name is None:
            raise NotFound(filename)
        sha256 = self._record(channel, subdir, filename)["sha256"]
        return self._get(f"{self.namespace}/{channel}/{subdir}/{oci_name(name)}", f"blobs/sha256:{sha256}")


def make_handler(channels):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            if os.environ.get("GHCR_BRIDGE_VERBOSE"):
                sys.stderr.write("ghcr-bridge: " + fmt % args + "\n")

        def _reply(self, status, body=b"", content_type="text/plain", extra=None, send_body=True):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            self.end_headers()
            if send_body:
                self.wfile.write(body)

        def _serve(self, send_body):
            path = urllib.parse.unquote(urllib.parse.urlparse(self.path).path).strip("/")
            if path == "healthz":
                return self._reply(200, b"ok", send_body=send_body)
            parts = path.split("/")
            if len(parts) != 3:
                return self._reply(404, b"not found", send_body=send_body)
            channel, subdir, filename = parts
            try:
                if filename == "repodata.json":
                    data, digest = channels.repodata(channel, subdir)
                    etag = f'"{digest}"'
                    if self.headers.get("If-None-Match") == etag:
                        return self._reply(304, extra={"ETag": etag}, send_body=False)
                    return self._reply(200, data, "application/json",
                                       {"ETag": etag, "Cache-Control": "no-cache"}, send_body)
                if package_name(filename) is None:
                    return self._reply(404, b"not found", send_body=send_body)   # .zst, shards, jlap: use repodata.json
                with channels.package(channel, subdir, filename) as upstream:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    length = upstream.headers.get("Content-Length")
                    if length:
                        self.send_header("Content-Length", length)
                    self.end_headers()
                    if send_body:
                        while chunk := upstream.read(1 << 20):
                            self.wfile.write(chunk)
            except NotFound as e:
                self._reply(404, str(e).encode(), send_body=send_body)
            except (OSError, ValueError) as e:
                sys.stderr.write(f"ghcr-bridge: {self.path}: {e}\n")
                try:
                    self._reply(502, str(e).encode(), send_body=send_body)
                except OSError:
                    pass

        def do_GET(self):
            self._serve(send_body=True)

        def do_HEAD(self):
            self._serve(send_body=False)

    return Handler


def _already_running(port):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=2) as r:
            return r.read() == b"ok"
    except OSError:
        return False


@contextlib.contextmanager
def running(port=DEFAULT_PORT, channels=None):
    """Serve the relay in a background thread; yields its base URL.

    Reuses a relay already listening on `port`, and falls back to a free port
    if `port` is taken by something else.
    """
    if _already_running(port):
        yield f"http://127.0.0.1:{port}"
        return
    handler = make_handler(channels or OciChannels())
    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    except OSError:
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def child_env():
    """Environment for a client of the relay: never send 127.0.0.1 through a proxy."""
    env = dict(os.environ)
    for key in ("NO_PROXY", "no_proxy"):
        values = [v for v in env.get(key, "").split(",") if v]
        env[key] = ",".join(values + [h for h in ("127.0.0.1", "localhost") if h not in values])
    return env


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m emci.ghcr_bridge", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="serve until interrupted")
    run = sub.add_parser("run", help="serve while running a command, e.g. run -- rattler-build build ...")
    for p in (serve, run):
        p.add_argument("--port", type=int, default=DEFAULT_PORT)
    run.add_argument("cmd", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)

    if args.command == "serve":
        with running(args.port) as url:
            print(f"Serving ghcr.io/{default_namespace()} channels at {url}/<channel>", flush=True)
            try:
                while True:
                    time.sleep(3600)
            except KeyboardInterrupt:
                return 0
    cmd = args.cmd[1:] if args.cmd[:1] == ["--"] else args.cmd
    if not cmd:
        parser.error("run needs a command, e.g. run -- rattler-build build ...")
    with running(args.port) as url:
        # the command was written for the default port; point it at the real one
        cmd = [c.replace(f"http://127.0.0.1:{DEFAULT_PORT}", url) for c in cmd]
        return subprocess.run(cmd, env=child_env()).returncode


if __name__ == "__main__":
    sys.exit(main())
