"""Zip the built exe folder for a GitHub release.

    uv run python packaging/release.py [dist/launcher.dist]

Writes ``dist/hiro-budget-<version>-windows.zip`` with one top folder
``hiro-budget-<version>/``. Checks first that the folder holds what a release needs.
"""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

from hiro_budget import __version__

ROOT = Path(__file__).resolve().parent.parent
REQUIRED = [
    "hiro-budget.exe",
    "Start demo.cmd",
    "LICENSE.txt",
    "THIRD_PARTY_LICENSES.txt",
    "demo/config/config.yaml",
    "demo/data/demo.db",
    "fints/__init__.py",
    "hiro_budget/templates/config.example.yaml",
]


def main(argv: list[str]) -> int:
    dist = Path(argv[0]) if argv else ROOT / "dist" / "launcher.dist"
    missing = [f for f in REQUIRED if not (dist / f).is_file()]
    if missing:
        print(f"not a complete build in {dist}: missing {', '.join(missing)}")
        return 1
    if (dist / "config").exists() or (dist / "data").exists():
        print(f"{dist} contains config/ or data/ (someone's own data): build a fresh folder")
        return 1
    top = f"hiro-budget-{__version__}"
    out = dist.parent / f"{top}-windows.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for f in sorted(p for p in dist.rglob("*") if p.is_file()):
            z.write(f, f"{top}/{f.relative_to(dist).as_posix()}")
    print(f"{out}: {out.stat().st_size / 1e6:.1f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
