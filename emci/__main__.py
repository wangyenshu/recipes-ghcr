from .rattler_build import build_with_rattler
from .constants import (
    RECIPES_SUBDIR_MAPPING,
    RECIPES_EMSCRIPTEN_DIR,
    RECIPES_WASM_DIR,
)
from .find_recipes_with_changes import find_recipes_with_changes
from .playwright import changed_recipes_need_playwright, recipe_needs_playwright
from .lint import lint_recipe_file, lint_recipes
from .upload import extract_channel_from_pkg

import sys
import json
import os
import tempfile
import shutil
from pathlib import Path

from typing import List, Optional
import typer

app = typer.Typer(pretty_exceptions_enable=False)
build_app = typer.Typer()
app.add_typer(build_app, name="build")


upload_app = typer.Typer()
app.add_typer(upload_app, name="upload")

@upload_app.command()
def extract_channel(pkg_file):
    print(extract_channel_from_pkg(pkg_file))


@upload_app.command()
def ghcr(
    output_dir: str,
    platform: List[str] = typer.Option(..., help="subdir of OUTPUT_DIR to publish, repeatable"),
    registry: str = typer.Option("ghcr.io/wangyenshu", help="registry namespace the channels live under"),
    source_url: Optional[str] = typer.Option(None, help="repository URL to link the packages to"),
    plain_http: bool = typer.Option(False, help="talk plain HTTP to the registry (local testing only)"),
    overwrite: bool = typer.Option(False, help="replace packages already published with the same version and build number"),
):
    """Push the built packages to an OCI registry and update each channel's repodata.json."""
    from .ghcr import ChannelPublisher

    packages = []
    for subdir in platform:
        for pkg in sorted((Path(output_dir) / subdir).glob("*.tar.bz2")):
            channel = extract_channel_from_pkg(str(pkg))
            print(f"{pkg.name} -> channel {channel}")
            packages.append((channel, pkg))

    if not packages:
        print("No packages to upload")
        return

    publisher = ChannelPublisher(registry, source_url=source_url, plain_http=plain_http, overwrite=overwrite)
    pushed, failed = publisher.publish(packages)

    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a") as f:
            f.write("pushed<<EOF\n")
            f.write("".join(f"{p}\n" for p in pushed))
            f.write("EOF\n")

    skipped = sum(1 for _, p in packages if p not in pushed and p not in failed)
    print(f"{len(pushed)} pushed, {skipped} already published, {len(set(failed))} failed")
    if failed:
        raise typer.Exit(code=1)


# -- copying existing channels to the registry (see emci/seed.py) --------------

SEED_SOURCE = "https://repo.prefix.dev"
SEED_CHANNELS = "emscripten-forge-4x emscripten-forge-4x-experimental"


@upload_app.command()
def seed_plan(
    channels: str = typer.Option(SEED_CHANNELS, help="space-separated channel names"),
    source: str = typer.Option(SEED_SOURCE, help="base URL of the source channels"),
):
    """Print the push jobs (JSON) for copying CHANNELS from SOURCE; also written to GITHUB_OUTPUT as `matrix`."""
    from .seed import plan

    jobs = plan(source, channels.split())
    matrix = json.dumps({"include": jobs})
    print(matrix)
    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a") as f:
            f.write(f"matrix={matrix}\n")


@upload_app.command()
def seed_push(
    channel: str,
    subdir: str,
    shard: int = typer.Option(0),
    shards: int = typer.Option(1),
    out: str = typer.Option("seed-result.json", help="where to write the result for seed-index"),
    source: str = typer.Option(SEED_SOURCE),
    registry: str = typer.Option("ghcr.io/wangyenshu"),
    source_url: Optional[str] = typer.Option(None, help="repository URL to link the packages to"),
    workers: int = typer.Option(4, help="packages copied in parallel"),
    plain_http: bool = typer.Option(False, help="local testing only"),
):
    """Copy one shard of SOURCE/CHANNEL/SUBDIR to the registry (packages only, no index)."""
    from .ghcr import ChannelPublisher
    from .seed import push_shard

    publisher = ChannelPublisher(registry, source_url=source_url, plain_http=plain_http)
    result = push_shard(publisher, source, channel, subdir, shard, shards, workers=workers)
    Path(out).write_text(json.dumps(result))
    print(f"{len(result['records'])} ready to index, {len(result['kept'])} kept from the registry, "
          f"{len(result['failed'])} failed")
    if result["failed"]:
        raise typer.Exit(code=1)


@upload_app.command()
def seed_index(
    results_dir: str,
    registry: str = typer.Option("ghcr.io/wangyenshu"),
    source_url: Optional[str] = typer.Option(None, help="repository URL to link the indexes to"),
    plain_http: bool = typer.Option(False, help="local testing only"),
):
    """Add every package from the seed-push results in RESULTS_DIR to the registry indexes."""
    from .ghcr import ChannelPublisher
    from .seed import index

    results = [json.loads(f.read_text()) for f in sorted(Path(results_dir).rglob("*.json"))]
    print(f"{len(results)} result file(s), {sum(len(r['records']) for r in results)} package(s)")
    publisher = ChannelPublisher(registry, source_url=source_url, plain_http=plain_http)
    failed = index(publisher, results)
    if failed:
        raise typer.Exit(code=1)

@build_app.command()
def changed(
    root_dir,
    old,
    new,
    dryrun: Optional[bool] = typer.Option(False),
    skip_tests: Optional[bool] = typer.Option(False),
    skip_existing: Optional[bool] = typer.Option(True)
):
    work_dir = os.getcwd()
    recipes_dir = os.path.join(root_dir, "recipes")
    recipes_with_changes_per_subdir = find_recipes_with_changes(old=old, new=new)

    for subdir, recipe_with_changes in recipes_with_changes_per_subdir.items():
        if len(recipe_with_changes) == 0:
            continue
        # create a  temp dir and copy all changed recipes
        # to that dir (because Then we can let boa do the
        # topological sorting)
        with tempfile.TemporaryDirectory() as tmp_folder_root:
            tmp_recipes_root_str = os.path.join(
                tmp_folder_root, "recipes", "recipes_per_platform"
            )
            os.makedirs(tmp_folder_root, exist_ok=True)

            for recipe_with_change in recipe_with_changes:

                recipe_dir = os.path.join(recipes_dir, subdir, recipe_with_change)

                # diff can shown deleted recipe as changed
                if os.path.isdir(recipe_dir):
                    tmp_recipe_dir = os.path.join(
                        tmp_recipes_root_str, recipe_with_change
                    )
                    # os.mkdir(tmp_recipe_dir)
                    shutil.copytree(recipe_dir, tmp_recipe_dir)

            print([x[0] for x in os.walk(tmp_recipes_root_str)])

            # delete all potential "recipe_legacy.yaml" files
            for root, dirs, files in os.walk(tmp_recipes_root_str):
                for file in files:
                    if file == "recipe_legacy.yaml":
                        os.remove(os.path.join(root, file))
            build_with_rattler(recipe=None, recipes_dir=tmp_recipes_root_str, emscripten_wasm32=RECIPES_SUBDIR_MAPPING[subdir] == "emscripten-wasm32")


bot_app = typer.Typer()
app.add_typer(bot_app, name="bot")


@bot_app.command()
def bump_recipes_versions(target_branch_name: str):
    from .bot.bump_recipes_versions import bump_recipe_versions

    # Branch switch happens inside bump_recipe_versions
    if target_branch_name == "emscripten-6x":
        recipe_dirs = [RECIPES_WASM_DIR]
    else:
        recipe_dirs = [RECIPES_EMSCRIPTEN_DIR]

    for recipe_dir in recipe_dirs:
        bump_recipe_versions(recipe_dir, target_branch_name)


@bot_app.command()
def update_matplotlib_fontcache(target_branch_name: str):
    from .bot.update_matplotlib_fontcache import update_matplotlib_fontcache

    update_matplotlib_fontcache(RECIPES_EMSCRIPTEN_DIR, target_branch_name)


def find_named_recipes(root_dir, names):
    """{recipes subdir: [recipe dir]} for recipe NAMES (space or comma separated)."""
    wanted = [n for n in names.replace(",", " ").split() if n]
    found, missing = {}, []
    for name in wanted:
        dirs = [Path(root_dir) / "recipes" / subdir / name for subdir in RECIPES_SUBDIR_MAPPING]
        dirs = [d for d in dirs if (d / "recipe.yaml").is_file()]
        if not dirs:
            missing.append(name)
        for d in dirs:
            found.setdefault(d.parent.name, []).append(d)
    if missing:
        raise typer.BadParameter(f"no recipe named {', '.join(missing)} under recipes/")
    # same order as the normal build: host-platform recipes first, then emscripten ones
    return {subdir: found[subdir] for subdir in RECIPES_SUBDIR_MAPPING if subdir in found}


@build_app.command()
def explicit(
    root_dir,
    names: str = typer.Argument(..., help="recipe names, space or comma separated"),
    only: str = typer.Option("all", help="build only recipes from this folder: recipes, recipes_emscripten or all"),
    skip_existing: str = typer.Option("local", help="rattler-build --skip-existing"),
):
    """Build the named recipes (e.g. "suitesparse openjdk8"), whether or not they changed."""
    for subdir, recipe_dirs in find_named_recipes(root_dir, names).items():
        if only != "all" and subdir != only:
            print(f"Skipping {', '.join(d.name for d in recipe_dirs)} ({subdir} not built here)")
            continue
        with tempfile.TemporaryDirectory() as tmp_folder_root:
            tmp_recipes_root = Path(tmp_folder_root) / "recipes" / "recipes_per_platform"
            for recipe_dir in recipe_dirs:
                shutil.copytree(recipe_dir, tmp_recipes_root / recipe_dir.name)
            for legacy in tmp_recipes_root.rglob("recipe_legacy.yaml"):
                legacy.unlink()
            print(f"Building {', '.join(d.name for d in recipe_dirs)} from {subdir}")
            build_with_rattler(
                recipe=None,
                recipes_dir=str(tmp_recipes_root),
                emscripten_wasm32=RECIPES_SUBDIR_MAPPING[subdir] == "emscripten-wasm32",
                skip_existing=skip_existing,
            )


@build_app.command("explicit-needs-playwright")
def explicit_needs_playwright(root_dir, names: str):
    """Print true/false depending on whether the named recipes need playwright."""
    dirs = [d for ds in find_named_recipes(root_dir, names).values() for d in ds]
    print("true" if any(recipe_needs_playwright((d / "recipe.yaml").read_text()) for d in dirs) else "false")


@build_app.command("needs-playwright")
def needs_playwright(old: str, new: str):
    """Print true/false depending on whether changed recipes need playwright."""
    if changed_recipes_need_playwright(old, new):
        print("true")
    else:
        print("false")


@app.command()
def lint(
    old: Optional[str] = typer.Argument(None, help="Git ref for the old commit"),
    new: Optional[str] = typer.Argument(None, help="Git ref for the new commit"),
    file: Optional[Path] = typer.Option(
        None,
        "--file",
        help="Path to a recipe.yaml or a recipe directory containing recipe.yaml",
    ),
):
    """
    Validate recipe metadata using Pydantic schema.

    Lint changed recipes between two git refs, or a single recipe with --file.
    Exits with code 1 if any recipe fails validation.
    """
    if file is not None:
        if old is not None or new is not None:
            print("❌ Cannot use --file together with old/new git refs", file=sys.stderr)
            raise typer.Exit(1)
        if not lint_recipe_file(file):
            raise typer.Exit(1)
        return

    if old is None or new is None:
        print(
            "❌ Provide both old and new git refs, or use --file to lint a single recipe",
            file=sys.stderr,
        )
        raise typer.Exit(1)

    lint_recipes(old, new)


@app.command()
def sync(
    migration_ref: str = typer.Argument(
        ...,
        help="Migration branch as remote/branch or branch (default remote: origin)",
    ),
    old: str = typer.Argument(..., help="Git ref for the old commit"),
    new: str = typer.Argument(..., help="Git ref for the new commit"),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Apply file changes on a new branch and print the PR title/body without committing or opening a PR",
    ),
):
    """Sync recipe changes from main onto a migration branch and open a PR."""
    from .sync_migration_branch import sync_migration_branch

    sync_migration_branch(migration_ref, old, new, dry_run=dry_run)


if __name__ == "__main__":
    app()
