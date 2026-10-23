import platform
import os
import subprocess
from pathlib import Path
from .constants import RATTLER_CONDA_BUILD_CONFIG_PATH, DEFAULT_EMSCRIPTEN_FORGE_CHANNEL
from . import ghcr_bridge


def build_with_rattler(recipe=None, recipes_dir=None, emscripten_wasm32=False, skip_existing="local"):

    cmd = ["rattler-build", "build", "--package-format", "tar-bz2", "--log-style", "simple"]

    # build single recipe or all recipes in a directory ?
    if recipe is not None and recipes_dir is not None:
        raise ValueError("recipe and recipes_dir cannot be both set")
    elif recipe is None and recipes_dir is None:
        raise ValueError("recipe or recipes_dir must be set")
    elif recipe is not None:
        cmd.extend(["--recipe", str(recipe)])
    elif recipes_dir is not None:
        cmd.extend(["--recipe-dir", str(recipes_dir)])
        recipes_path = Path(recipes_dir)
        if recipes_path.is_dir():
            folder_names = {p.name for p in recipes_path.iterdir() if p.is_dir()}

    cmd.extend(["--skip-existing", skip_existing])

    # build for emscripten-wasm32?
    if emscripten_wasm32:
        cmd.extend(["--target-platform", "emscripten-wasm32"])
        # cmd.extend(["--variant-config", str(VARIANT_CONFIG_PATH)])

    cmd.extend(["-m", RATTLER_CONDA_BUILD_CONFIG_PATH])

    # emscripten-forge packages come from ghcr.io/<owner>/emscripten-forge-4x, which
    # rattler-build can't read directly (no oci:// support), so it goes through a
    # local relay that serves the ghcr channel over plain HTTP while the build runs
    with ghcr_bridge.running() as relay:
        cmd.extend([
            "-c", f"{relay}/{DEFAULT_EMSCRIPTEN_FORGE_CHANNEL}",
            "-c", "microsoft",
            "-c", "conda-forge",
            "-c", "bioconda"
        ])

        print(f"Running rattler-build with command: {cmd}")
        print(f"(channel {DEFAULT_EMSCRIPTEN_FORGE_CHANNEL} is relayed from oci://ghcr.io/{ghcr_bridge.default_namespace()}/{DEFAULT_EMSCRIPTEN_FORGE_CHANNEL})")

        ret = subprocess.run(' '.join(cmd), check=False, shell=True, env=ghcr_bridge.child_env())
    if ret.returncode != 0:
        raise RuntimeError(f"rattler-build failed with return code {ret.returncode}")

