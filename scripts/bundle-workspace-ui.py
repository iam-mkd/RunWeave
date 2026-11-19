"""Copy the built workspace UI into the package so wheels serve it without Node.

Run after `npm --prefix frontend run build` and before `uv build`:

    python scripts/bundle-workspace-ui.py

The copy (src/runweave/workspace_ui/) is not committed; a source checkout serves
frontend/dist directly. Stale files from an earlier build are removed.
"""

import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source", type=Path, default=ROOT / "frontend" / "dist")
    parser.add_argument("--target", type=Path, default=ROOT / "src" / "runweave" / "workspace_ui")
    args = parser.parse_args()
    source: Path = args.source
    target: Path = args.target
    if not (source / "index.html").is_file() or not (source / "assets").is_dir():
        print(
            f"No built workspace in {source}; run `npm --prefix frontend run build` first.",
            file=sys.stderr,
        )
        return 1
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(source, target)
    files = sum(1 for path in target.rglob("*") if path.is_file())
    print(f"Bundled {files} workspace files into {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
