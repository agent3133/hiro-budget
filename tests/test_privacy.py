"""No personal data in what is committed for tests, plugins and docs (#24).

Contributors add fixtures for their bank's statements; real IBANs or e-mail addresses must
not get in. A real IBAN has a valid checksum (ISO 13616, mod 97); made-up ones such as
``DE12345678901234567890`` usually do not. E-mail addresses must use an example domain.
"""

from __future__ import annotations

import re
from pathlib import Path

from tests.conftest import REPO_ROOT

IBAN = re.compile(r"\b([A-Z]{2}\d{2}(?: ?[0-9A-Z]{4}){3,7}(?: ?[0-9A-Z]{1,3})?)\b")
EMAIL = re.compile(r"\b[\w.+-]+@([\w-]+(?:\.[\w-]+)+)\b")
ALLOWED_DOMAINS = re.compile(r"(^|\.)example\.(org|com|net)$|^anthropic\.com$")
SCANNED = ("tests", "src/hiro_budget/plugins", "docs")
SUFFIXES = {".py", ".yaml", ".md", ".txt", ".csv"}


def valid_iban(text: str) -> bool:
    s = text.replace(" ", "")
    number = int("".join(str(int(c, 36)) for c in s[4:] + s[:4]))
    return number % 97 == 1


def scanned_files() -> list[Path]:
    return [
        p
        for root in SCANNED
        for p in (REPO_ROOT / root).rglob("*")
        if p.suffix in SUFFIXES and "__pycache__" not in p.parts and p.name != Path(__file__).name
    ]


def test_iban_checksum():
    assert valid_iban("GB82 WEST 1234 5698 7654 32")  # the standard's example
    assert not valid_iban("DE12345678901234567890")


def test_no_real_ibans_in_tests_plugins_and_docs():
    found = [
        f"{p.relative_to(REPO_ROOT)}: {m.group(1)[:4]}…"
        for p in scanned_files()
        for m in IBAN.finditer(p.read_text(encoding="utf-8", errors="ignore"))
        if valid_iban(m.group(1))
    ]
    assert found == [], "IBANs with a valid checksum (real?): use made-up ones"


def test_only_example_email_addresses():
    found = [
        f"{p.relative_to(REPO_ROOT)}: …@{m.group(1)}"
        for p in scanned_files()
        for m in EMAIL.finditer(p.read_text(encoding="utf-8", errors="ignore"))
        if not ALLOWED_DOMAINS.search(m.group(1))
    ]
    assert found == [], "use addresses at example.org / example.com"
