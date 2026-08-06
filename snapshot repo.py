"""snapshot_repo.py — zip every .py file in a repo so it can be shared.

USAGE
    python snapshot_repo.py
    python snapshot_repo.py "C:\\path\\to\\repo"
    python snapshot_repo.py --out "C:\\Users\\lane.white\\Downloads\\snapshot.zip"

With no argument it snapshots the folder this script lives in (so dropping it
at the top of the repo and double-clicking it just works). Output defaults to
a timestamped zip in your Downloads folder, so it lands wherever you'd go to
grab a file to upload.

WHAT IT DOES
    Walks the repo, collects every *.py file, and zips them preserving their
    relative folder structure (so pythonrfparts/gui.py stays distinguishable
    from a top-level gui.py of the same name). Skips virtual environments,
    __pycache__, .git, and anything that looks like a dependency folder, so the
    zip is your code, not the world.

WHAT IT DOESN'T DO
    No filtering by "changed" -- it takes everything, since the point is to let
    someone else diff against what they already have. Nothing is modified;
    this only reads the repo and writes the zip.
"""
from __future__ import annotations

import argparse
import fnmatch
import sys
import time
import zipfile
from pathlib import Path

# Directories never worth including: virtual envs, caches, VCS metadata, and
# dependency trees that happen to sit inside the repo.
_SKIP_DIRS = {
    ".git", ".hg", ".svn", "__pycache__", ".mypy_cache", ".pytest_cache",
    ".venv", "venv", "env", ".env", "site-packages", "node_modules",
    ".idea", ".vscode", "build", "dist", "*.egg-info",
}


def _should_skip_dir(name: str) -> bool:
    return any(fnmatch.fnmatch(name, pat) for pat in _SKIP_DIRS)


def find_py_files(root: Path):
    """Every *.py file under root, skipping the directories above.

    Walks by hand rather than Path.rglob so a skipped directory's subtree is
    never even listed -- rglob would still descend into a venv before the
    caller gets a chance to filter it out, which is slow on a repo with a large
    virtual environment sitting inside it.
    """
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            entries = list(d.iterdir())
        except OSError as e:
            print(f"  ! could not list {d}: {e}")
            continue
        for entry in entries:
            try:
                is_dir = entry.is_dir()
            except OSError:
                continue
            if is_dir:
                if not _should_skip_dir(entry.name):
                    stack.append(entry)
                continue
            if entry.suffix == ".py":
                yield entry


def default_downloads_dir() -> Path:
    d = Path.home() / "Downloads"
    return d if d.is_dir() else Path.home()


def make_snapshot(repo: Path, out_zip: Path) -> tuple[int, int]:
    """Zip every .py file under repo into out_zip. Returns (count, bytes)."""
    files = sorted(find_py_files(repo))
    out_zip.parent.mkdir(parents=True, exist_ok=True)
    total_bytes = 0
    with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in files:
            try:
                rel = f.relative_to(repo)
            except ValueError:
                rel = f.name
            try:
                zf.write(f, arcname=str(rel))
                total_bytes += f.stat().st_size
            except OSError as e:
                print(f"  ! skipped {f}: {e}")
    return len(files), total_bytes


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("repo", nargs="?", default=None,
                    help="folder to snapshot (default: the folder this script "
                         "is in)")
    ap.add_argument("--out", default=None,
                    help="output .zip path (default: a timestamped file in "
                         "your Downloads folder)")
    args = ap.parse_args(argv)

    repo = Path(args.repo).expanduser().resolve() if args.repo else \
        Path(__file__).resolve().parent
    if not repo.is_dir():
        sys.exit(f"Not a folder: {repo}")

    if args.out:
        out_zip = Path(args.out).expanduser()
        if out_zip.is_dir():
            out_zip = out_zip / f"{repo.name}_snapshot_{time.strftime('%Y%m%d_%H%M%S')}.zip"
    else:
        out_zip = (default_downloads_dir()
                  / f"{repo.name}_snapshot_{time.strftime('%Y%m%d_%H%M%S')}.zip")

    print(f"repo   : {repo}")
    print(f"output : {out_zip}")

    n, size = make_snapshot(repo, out_zip)
    if n == 0:
        print("\n! no .py files found. Is the repo path right?")
        return 1

    print(f"\nzipped {n} .py file(s), {size / 1024:.0f} KB uncompressed")
    print(f"      -> {out_zip}  ({out_zip.stat().st_size / 1024:.0f} KB)")
    print("\nUpload that zip to hand over the current state of your code.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
