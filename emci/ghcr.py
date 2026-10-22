"""Publish conda packages to an OCI registry such as ghcr.io.

The layout matches what rattler (rattler-build, pixi, mamba via rattler) reads
from an ``oci://`` channel, e.g. ``oci://ghcr.io/wangyenshu/emscripten-forge-4x``:

* a package ``<name>-<version>-<build>.tar.bz2`` (or ``.conda``) in ``<subdir>`` is the
  single layer (media type ``application/vnd.conda.package.v1``, ``.v2`` for ``.conda``) of
  ``<registry>/<channel>/<subdir>/<name>:<version>-<build>``
  (``+ ! =`` in the tag are escaped, a leading ``_`` in the name becomes ``zzz_``)
* the index of a subdir is the layer (``application/vnd.conda.repodata.v1+json``)
  of ``<registry>/<channel>/<subdir>/repodata.json:latest``

A registry does no indexing of its own, so after pushing packages we pull the
subdir's ``repodata.json``, add the new records and push it back. Registries
have no compare-and-swap, so the update is verified and retried in case
another run updated the same index at the same time.
"""

import hashlib
import json
import random
import subprocess
import tarfile
import tempfile
import time
from collections import defaultdict
from pathlib import Path

# archive extension -> (repodata.json key, OCI layer media type)
ARCHIVE_TYPES = {
    ".tar.bz2": ("packages", "application/vnd.conda.package.v1"),
    ".conda": ("packages.conda", "application/vnd.conda.package.v2"),
}
REPODATA_MEDIA_TYPE = "application/vnd.conda.repodata.v1+json"
SOURCE_ANNOTATION = "org.opencontainers.image.source"


class PublishError(RuntimeError):
    pass


class ConflictError(PublishError):
    """The registry already has this package version with different content."""


def archive_type(filename):
    for ext, kind in ARCHIVE_TYPES.items():
        if filename.endswith(ext):
            return ext, kind
    raise ValueError(f"{filename}: not a conda package (.tar.bz2 or .conda)")


def repodata_key(filename):
    return archive_type(filename)[1][0]


def package_media_type(filename):
    return archive_type(filename)[1][1]


def split_package_filename(filename):
    ext, _ = archive_type(filename)
    name, version, build = filename[: -len(ext)].rsplit("-", 2)
    return name, version, build


def oci_tag(version, build):
    # OCI tags can't contain `+`, `!` or `=`; same escaping as rattler's oci middleware
    tag = f"{version}-{build}"
    return tag.replace("+", "__p__").replace("!", "__e__").replace("=", "__eq__")


def oci_name(name):
    # OCI repository names can't start with `_`
    return f"zzz{name}" if name.startswith("_") else name


def package_ref(registry, channel, subdir, filename):
    name, version, build = split_package_filename(filename)
    return f"{registry}/{channel}/{subdir}/{oci_name(name)}:{oci_tag(version, build)}"


def repodata_ref(registry, channel, subdir):
    return f"{registry}/{channel}/{subdir}/repodata.json:latest"


def repodata_record(pkg_path):
    """The repodata.json entry for a package: info/index.json + size and hashes."""
    pkg_path = Path(pkg_path)
    with tarfile.open(pkg_path, "r:bz2") as tar:
        member = tar.extractfile("info/index.json")
        if member is None:
            raise PublishError(f"{pkg_path.name} has no info/index.json")
        record = json.load(member)

    data = pkg_path.read_bytes()
    record["md5"] = hashlib.md5(data).hexdigest()
    record["sha256"] = hashlib.sha256(data).hexdigest()
    record["size"] = len(data)
    return record


def empty_repodata(subdir):
    return {
        "info": {"subdir": subdir},
        "packages": {},
        "packages.conda": {},
        "removed": [],
        "repodata_version": 1,
    }


NOT_FOUND = ("not found", "manifest_unknown", "name_unknown")


class Oras:
    def __init__(self, plain_http=False, attempts=4):
        self.extra = ["--plain-http"] if plain_http else []
        self.attempts = attempts

    def run(self, *args, cwd=None, check=True):
        """Run oras, retrying failures other than "not found" (rate limits, network)."""
        cmd = ["oras", *args, *self.extra]
        for attempt in range(1, self.attempts + 1):
            proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
            if proc.returncode == 0 or any(s in proc.stderr.lower() for s in NOT_FOUND):
                break
            if attempt < self.attempts:
                time.sleep(2 ** attempt + random.uniform(0, 2))
        if check and proc.returncode != 0:
            raise PublishError(f"{' '.join(cmd)} failed:\n{proc.stderr.strip()}")
        return proc

    def manifest(self, ref):
        """The manifest of `ref`, or None if it doesn't exist."""
        proc = self.run("manifest", "fetch", ref, check=False)
        if proc.returncode == 0:
            return json.loads(proc.stdout)
        stderr = proc.stderr.lower()
        if any(s in stderr for s in NOT_FOUND):
            return None
        # anything else (auth, network) must not be mistaken for "doesn't exist":
        # that would publish an index without the existing packages
        raise PublishError(f"oras manifest fetch {ref} failed:\n{proc.stderr.strip()}")

    def push(self, ref, path, media_type, annotations):
        path = Path(path)
        args = ["push", ref, f"{path.name}:{media_type}"]
        for key, value in annotations.items():
            args += ["--annotation", f"{key}={value}"]
        self.run(*args, cwd=path.parent)

    def pull(self, ref, out_dir):
        self.run("pull", ref, "-o", str(out_dir))


class ChannelPublisher:
    def __init__(self, registry, source_url=None, plain_http=False, retries=6, overwrite=False):
        """`overwrite`: replace a package already in the registry with different content
        (a rebuild with the same version and build number). Off by default, because
        anyone who pinned the old file (e.g. in a lock file) gets a hash mismatch."""
        self.registry = registry.rstrip("/")
        self.overwrite = overwrite
        self.oras = Oras(plain_http=plain_http)
        self.annotations = {SOURCE_ANNOTATION: source_url} if source_url else {}
        self.retries = retries

    # -- repodata ---------------------------------------------------------

    def fetch_repodata(self, channel, subdir):
        ref = repodata_ref(self.registry, channel, subdir)
        if self.oras.manifest(ref) is None:
            return None
        with tempfile.TemporaryDirectory() as tmp:
            self.oras.pull(ref, tmp)
            return json.loads((Path(tmp) / "repodata.json").read_text())

    def push_repodata(self, channel, subdir, repodata):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "repodata.json"
            path.write_text(json.dumps(repodata, indent=1, sort_keys=True))
            self.oras.push(
                repodata_ref(self.registry, channel, subdir),
                path,
                REPODATA_MEDIA_TYPE,
                self.annotations,
            )

    def ensure_subdir(self, channel, subdir):
        """Create an empty index for `subdir` if there is none (clients need `noarch`)."""
        if self.fetch_repodata(channel, subdir) is None:
            print(f"Creating empty {channel}/{subdir}/repodata.json")
            self.push_repodata(channel, subdir, empty_repodata(subdir))

    def add_to_repodata(self, channel, subdir, records, info=None):
        """Merge `records` ({filename: record}) into the subdir's index, verifying the result.

        `info` adds fields to the index's `info` section that it doesn't have yet
        (e.g. `channel_relations` copied from the source channel).
        """
        extra_info = {k: v for k, v in (info or {}).items() if k not in ("subdir", "base_url")}
        for attempt in range(1, self.retries + 1):
            repodata = self.fetch_repodata(channel, subdir) or empty_repodata(subdir)
            missing = {
                fn: rec
                for fn, rec in records.items()
                if repodata.get(repodata_key(fn), {}).get(fn, {}).get("sha256") != rec["sha256"]
            }
            info_missing = {k: v for k, v in extra_info.items() if k not in repodata.get("info", {})}
            if not missing and not info_missing:
                print(f"{channel}/{subdir}/repodata.json: {len(records)} package(s) indexed")
                return
            if attempt > 1:
                print(f"{len(missing)} record(s) missing from {channel}/{subdir}/repodata.json, retrying ({attempt}/{self.retries})")
            for fn, rec in missing.items():
                repodata.setdefault(repodata_key(fn), {})[fn] = rec
            repodata.setdefault("info", {}).update(info_missing)
            repodata["info"]["subdir"] = subdir
            self.push_repodata(channel, subdir, repodata)
            # another run may have pushed the same index in the meantime; wait, then verify
            time.sleep(random.uniform(5, 15))
        raise PublishError(f"could not update {channel}/{subdir}/repodata.json after {self.retries} attempts")

    # -- packages ---------------------------------------------------------

    def push_package(self, channel, subdir, pkg_path, record):
        """Push one package. Returns True if pushed, False if the identical package was already there."""
        ref = package_ref(self.registry, channel, subdir, pkg_path.name)
        manifest = self.oras.manifest(ref)
        if manifest is not None:
            digests = {layer["digest"] for layer in manifest.get("layers", [])}
            if f"sha256:{record['sha256']}" in digests:
                print(f"{pkg_path.name} already published, making sure it is indexed")
                return False
            if self.overwrite:
                print(f"Replacing {pkg_path.name} (different content, overwrite is on) -> {ref}")
                self.oras.push(ref, pkg_path, package_media_type(pkg_path.name), self.annotations)
                return True
            raise ConflictError(
                f"{ref} already exists with different content; refusing to overwrite "
                "(bump the build number instead)"
            )
        print(f"Pushing {pkg_path.name} -> {ref}")
        self.oras.push(ref, pkg_path, package_media_type(pkg_path.name), self.annotations)
        return True

    def publish(self, packages):
        """Publish `packages` ([(channel, path)]). Returns (pushed, failed) lists of paths."""
        by_index = defaultdict(dict)
        pushed, failed = [], []

        for channel, pkg_path in packages:
            pkg_path = Path(pkg_path)
            try:
                record = repodata_record(pkg_path)
                subdir = record["subdir"]
                if self.push_package(channel, subdir, pkg_path, record):
                    pushed.append(pkg_path)
                by_index[(channel, subdir)][pkg_path.name] = (pkg_path, record)
            except PublishError as e:
                print(f"Error: {e}")
                failed.append(pkg_path)

        for channel in sorted({channel for channel, _ in by_index}):
            self.ensure_subdir(channel, "noarch")

        for (channel, subdir), entries in sorted(by_index.items()):
            records = {fn: record for fn, (_, record) in entries.items()}
            try:
                self.add_to_repodata(channel, subdir, records)
            except PublishError as e:
                print(f"Error: {e}")
                failed.extend(path for path, _ in entries.values())
        return pushed, failed
