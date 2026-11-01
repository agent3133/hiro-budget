"""Write THIRD_PARTY_LICENSES.txt: name, version, licence and licence texts of every package
the app ships (the runtime dependencies and theirs), for the exe release.

    uv run python packaging/third_party_licenses.py [output file]
"""

from __future__ import annotations

import re
import sys
from importlib.metadata import PackageNotFoundError, distribution, requires
from pathlib import Path

PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"


def _name(requirement: str) -> str:
    return re.split(r"[<>=!~;\[ (]", requirement.strip(), maxsplit=1)[0].lower()


def runtime_closure() -> list[str]:
    """The runtime dependencies of the app, recursively (extras left out)."""
    seen: set[str] = set()
    import tomllib

    direct = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]["dependencies"]
    todo = [_name(r) for r in direct]
    while todo:
        name = todo.pop()
        if name in seen:
            continue
        try:
            reqs = requires(name) or []
        except PackageNotFoundError:
            continue  # a marker excluded it on this platform
        seen.add(name)
        todo += [_name(r) for r in reqs if "extra ==" not in r]
    return sorted(seen)


def licence_texts(dist) -> list[tuple[str, str]]:
    files = [
        f
        for f in dist.files or []
        if re.search(r"(LICEN[CS]E|COPYING|NOTICE|AUTHORS)", f.name, re.IGNORECASE)
        and f.suffix.lower() in ("", ".txt", ".md", ".rst")
    ]
    texts = []
    for f in sorted(files, key=lambda f: str(f)):
        try:
            texts.append((str(f), f.read_text(encoding="utf-8")))
        except (OSError, UnicodeDecodeError):
            continue
    return texts


def main(argv: list[str]) -> int:
    out = Path(argv[0]) if argv else Path("THIRD_PARTY_LICENSES.txt")
    parts = [
        "Hiro Budget ships the following third-party packages. Their licences follow.\n"
        "Packages under the LGPL (PySide6/Qt, python-fints and others marked so) are\n"
        "included as separate, replaceable files; their source is available from the\n"
        "projects named below (pypi.org).\n"
    ]
    missing = []
    for name in runtime_closure():
        dist = distribution(name)
        meta = dist.metadata
        licence = meta.get("License-Expression") or (meta.get("License") or "").splitlines()[0:1]
        if isinstance(licence, list):
            licence = licence[0] if licence else ""
        classifiers = [
            c.split(" :: ")[-1] for c in meta.get_all("Classifier") or [] if c.startswith("License")
        ]
        texts = licence_texts(dist)
        if not texts:
            missing.append(name)
        parts.append("=" * 78)
        parts.append(f"{meta['Name']} {meta['Version']}")
        parts.append(f"Licence: {licence or '; '.join(classifiers) or 'see the project'}")
        if url := meta.get("Home-page") or next(
            (u.split(", ", 1)[-1] for u in meta.get_all("Project-URL") or []), None
        ):
            parts.append(f"Project: {url}")
        for file, text in texts:
            parts.append(f"\n--- {file} ---\n{text.strip()}\n")
    out.write_text("\n".join(parts) + "\n", encoding="utf-8")
    print(f"{out}: {len(runtime_closure())} packages, without licence file: {missing or 'none'}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
