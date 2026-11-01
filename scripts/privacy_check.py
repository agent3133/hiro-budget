"""Check files for personal data before they are published (e.g. exported to GitHub).

    uv run python scripts/privacy_check.py            # the files tracked by git
    uv run python scripts/privacy_check.py some/dir   # every text file below a folder

Rules:
- every regular expression in ``.privacy-denylist`` (local, git-ignored: real names, IBANs,
  account numbers, employer, address; one per line, case-insensitive, ``#`` comments);
- IBANs with a valid checksum (made-up test IBANs have an invalid one);
- e-mail addresses outside ``example.org/.com/.net``.

Findings name file, line and rule, never the value. Exit code 1 if anything was found.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DENYLIST = ROOT / ".privacy-denylist"
IBAN = re.compile(r"\b([A-Z]{2}\d{2}(?: ?[0-9A-Z]{4}){3,7}(?: ?[0-9A-Z]{1,3})?)\b")
EMAIL = re.compile(r"\b[\w.+-]+@([\w-]+(?:\.[\w-]+)+)\b")
ALLOWED_EMAIL = re.compile(r"(^|\.)example\.(org|com|net)$|^anthropic\.com$")
# documented examples with a valid checksum, allowed on purpose
ALLOWED_IBANS = {"GB82WEST12345698765432"}  # ISO 13616's example (tests/test_privacy.py)
BINARY = {".qm", ".ico", ".png", ".svg", ".ttf", ".otf", ".pdf", ".db", ".npz", ".zip"}


def valid_iban(text: str) -> bool:
    s = text.replace(" ", "")
    return int("".join(str(int(c, 36)) for c in s[4:] + s[:4])) % 97 == 1


def denylist() -> list[tuple[int, re.Pattern[str]]]:
    if not DENYLIST.is_file():
        return []
    rules = []
    for n, line in enumerate(DENYLIST.read_text(encoding="utf-8").splitlines(), start=1):
        line = line.strip()
        if line and not line.startswith("#"):
            rules.append((n, re.compile(line, re.IGNORECASE)))
    return rules


def files(target: Path | None) -> list[Path]:
    if target is None:
        out = subprocess.run(
            ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True
        ).stdout
        return [ROOT / f for f in out.splitlines()]
    return [p for p in target.rglob("*") if p.is_file() and ".git" not in p.parts]


def check(paths: list[Path], base: Path) -> list[str]:
    rules = denylist()
    findings = []
    for path in paths:
        if path.suffix.lower() in BINARY or path.name == DENYLIST.name:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        name = path.relative_to(base) if path.is_relative_to(base) else path
        for lineno, line in enumerate(text.splitlines(), start=1):
            for n, rx in rules:
                if rx.search(line):
                    findings.append(f"{name}:{lineno}: denylist line {n}")
            for m in IBAN.finditer(line):
                iban = m.group(1).replace(" ", "")
                if valid_iban(iban) and iban not in ALLOWED_IBANS:
                    findings.append(f"{name}:{lineno}: IBAN with a valid checksum")
            for m in EMAIL.finditer(line):
                if not ALLOWED_EMAIL.search(m.group(1).lower()):
                    findings.append(f"{name}:{lineno}: e-mail address at {m.group(1)}")
    return findings


def main(argv: list[str]) -> int:
    target = Path(argv[0]).resolve() if argv else None
    paths = files(target)
    findings = check(paths, target or ROOT)
    if not DENYLIST.is_file():
        print(f"note: no {DENYLIST.name}: only IBAN checksums and e-mail domains checked")
    for f in findings:
        print(f)
    print(f"{len(paths)} files checked, {len(findings)} findings")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
