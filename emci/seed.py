"""Copy existing conda channels (e.g. the emscripten-forge channels on prefix.dev) to ghcr.io.

Three steps, each run by `.github/workflows/seed_ghcr.yaml`:

* `plan`:  look up which subdirs each source channel has and split the work into shards
* `push`:  one shard: download each package, check its sha256, push it to the registry
* `index`: add the copied packages to each subdir's repodata.json on the registry

The records written to the index are the source channel's own, unchanged (so any
repodata patches made there carry over). Re-running is safe: packages already on the
registry with the same content are skipped, and the index update is a merge.
If the registry already has a package with the same filename but different content
(rebuilt in this repo), the registry's version is kept and the source one is skipped.
"""

import hashlib
import json
import math
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

from .ghcr import ChannelPublisher, ConflictError, PublishError, package_ref

SUBDIRS = ["emscripten-wasm32", "noarch", "linux-64", "linux-aarch64", "osx-64", "osx-arm64", "win-64"]
MAX_FILES_PER_SHARD = 300
MAX_BYTES_PER_SHARD = 1_500_000_000


def _get(url, **kwargs):
    for attempt in range(1, 5):
        try:
            response = requests.get(url, timeout=120, **kwargs)
            if response.status_code < 500 and response.status_code != 429:
                return response
        except requests.RequestException:
            if attempt == 4:
                raise
        time.sleep(2 ** attempt)
    return response


def source_repodata(source, channel, subdir):
    """The source channel's repodata.json for `subdir`, or None if it has no such subdir."""
    response = _get(f"{source.rstrip('/')}/{channel}/{subdir}/repodata.json")
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return response.json()


def source_records(repodata):
    records = {}
    for key in ("packages", "packages.conda"):
        records.update(repodata.get(key, {}))
    return records


def shard_of(filename, shards):
    return int(hashlib.sha256(filename.encode()).hexdigest(), 16) % shards


# -- plan -------------------------------------------------------------------


def plan(source, channels):
    """The list of push jobs: one entry per (channel, subdir, shard)."""
    jobs = []
    for channel in channels:
        for subdir in SUBDIRS:
            repodata = source_repodata(source, channel, subdir)
            if repodata is None:
                continue
            records = source_records(repodata)
            if not records:
                continue
            size = sum(r.get("size", 0) for r in records.values())
            shards = max(1, math.ceil(len(records) / MAX_FILES_PER_SHARD), math.ceil(size / MAX_BYTES_PER_SHARD))
            print(f"{channel}/{subdir}: {len(records)} packages, {size / 1e9:.1f} GB, {shards} shard(s)")
            jobs += [{"channel": channel, "subdir": subdir, "shard": i, "shards": shards} for i in range(shards)]
    return jobs


# -- push -------------------------------------------------------------------


def download(url, dest, sha256):
    """Download `url` to `dest`, checking its sha256."""
    for attempt in range(1, 5):
        digest = hashlib.sha256()
        try:
            with requests.get(url, stream=True, timeout=300) as response:
                response.raise_for_status()
                with open(dest, "wb") as f:
                    for chunk in response.iter_content(chunk_size=1 << 20):
                        f.write(chunk)
                        digest.update(chunk)
            if digest.hexdigest() == sha256:
                return
            error = f"sha256 mismatch for {url}"
        except requests.RequestException as e:
            error = f"download of {url} failed: {e}"
        time.sleep(2 ** attempt)
    raise PublishError(error)


def push_shard(publisher, source, channel, subdir, shard, shards, workers=4):
    """Copy this shard's packages. Returns the result dict written for the index step."""
    repodata = source_repodata(source, channel, subdir)
    if repodata is None:
        raise PublishError(f"{source}/{channel}/{subdir} not found")
    records = {fn: r for fn, r in source_records(repodata).items() if shard_of(fn, shards) == shard}
    print(f"{channel}/{subdir} shard {shard + 1}/{shards}: {len(records)} packages")

    result = {
        "channel": channel,
        "subdir": subdir,
        "info": repodata.get("info", {}),
        "records": {},     # copied now, or already there with the same content
        "kept": [],        # registry already has a different build with this filename
        "failed": [],
    }

    def copy_one(filename, record):
        ref = package_ref(publisher.registry, channel, subdir, filename)
        manifest = publisher.oras.manifest(ref)
        if manifest is not None:
            digests = {layer["digest"] for layer in manifest.get("layers", [])}
            return "already" if f"sha256:{record['sha256']}" in digests else "kept"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / filename
            download(f"{source.rstrip('/')}/{channel}/{subdir}/{filename}", path, record["sha256"])
            publisher.push_package(channel, subdir, path, record)
        return "copied"

    counts = {"copied": 0, "already": 0, "kept": 0, "failed": 0}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(copy_one, fn, rec): fn for fn, rec in sorted(records.items())}
        for done, future in enumerate(as_completed(futures), 1):
            filename = futures[future]
            try:
                status = future.result()
            except ConflictError:
                status = "kept"
            except (PublishError, ValueError, OSError) as e:
                print(f"Error: {filename}: {e}")
                status = "failed"
            counts[status] += 1
            if status in ("copied", "already"):
                result["records"][filename] = records[filename]
            elif status == "kept":
                print(f"{filename}: registry has a different build with this name, keeping it")
                result["kept"].append(filename)
            else:
                result["failed"].append(filename)
            if done % 25 == 0 or done == len(records):
                print(f"  {done}/{len(records)} done ({counts})")
    return result


# -- index ------------------------------------------------------------------


def index(publisher, results):
    """Add the copied packages from all push results to the registry indexes."""
    merged = {}
    for result in results:
        key = (result["channel"], result["subdir"])
        entry = merged.setdefault(key, {"info": result.get("info", {}), "records": {}})
        entry["records"].update(result["records"])

    failed = []
    for channel in sorted({channel for channel, _ in merged}):
        publisher.ensure_subdir(channel, "noarch")
    for (channel, subdir), entry in sorted(merged.items()):
        try:
            publisher.add_to_repodata(channel, subdir, entry["records"], info=entry["info"])
        except PublishError as e:
            print(f"Error: {e}")
            failed.append(f"{channel}/{subdir}")
    return failed
