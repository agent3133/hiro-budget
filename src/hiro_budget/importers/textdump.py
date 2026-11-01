"""The text of a PDF as importers see it (``hiro-budget dump-pdf``), for writing a plugin.

``mask`` replaces what identifies a person and is easy to find: IBANs, e-mail addresses,
numbers of six digits or more. Dates and amounts stay, so the text still shows the layout.
Names, addresses and purpose texts are not found reliably: replace them by hand.
"""

from __future__ import annotations

import re
from pathlib import Path

IBAN = re.compile(r"\b([A-Z]{2})(\d{2}(?: ?[0-9A-Z]{4}){3,7}(?: ?[0-9A-Z]{1,3})?)\b")
EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")
LONG_NUMBER = re.compile(r"(?<![\d.,])\d{6,}(?![\d,])")


def _zeros(text: str) -> str:
    return re.sub(r"[0-9A-Z]", lambda m: "0" if m.group().isdigit() else "X", text)


def mask(text: str) -> str:
    text = IBAN.sub(lambda m: m.group(1) + _zeros(m.group(2)), text)
    text = EMAIL.sub("name@example.org", text)
    return LONG_NUMBER.sub(lambda m: "0" * len(m.group()), text)


def dump(path: Path, x_tolerance: float = 1.5, masked: bool = False) -> str:
    """All pages, each under a ``===== page N =====`` line."""
    from hiro_budget.plugin_api import pdf_page_texts

    parts = []
    for n, page in enumerate(pdf_page_texts(path, x_tolerance), start=1):
        parts.append(f"===== page {n} =====")
        parts.append(mask(page) if masked else page)
    return "\n".join(parts) + "\n"
