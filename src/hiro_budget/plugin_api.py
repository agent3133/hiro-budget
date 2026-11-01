"""What plugin code may use (plugin API version 1, see ``importers.plugins``).

Plugins import from this module only; everything else in the app may change. A plugin
declares ``api: 1`` in its ``plugin.yaml``.

An importer is a class with a ``name`` (stored with every import: never change it once
files were imported with it) and two methods::

    class MyBankPdfImporter:
        name = "mybank_pdf"

        def detect(self, path: Path) -> bool:   # cheap: is this file mine?
            ...

        def parse(self, path: Path) -> ParsedStatement:   # raise ImportError_ if not
            ...

The app then checks the balance, finds duplicates and overlaps, imports and categorizes.
Code of another plugin (one in ``requires``) is reached with ``module("plugin", "module")``.
"""

from __future__ import annotations

import types
from pathlib import Path

from hiro_budget.importers.base import (
    Importer,
    ImportError_,
    ParsedHolding,
    ParsedLink,
    ParsedStatement,
    ParsedTransaction,
)
from hiro_budget.money import parse_german_amount

API_VERSION = 1

__all__ = [
    "API_VERSION",
    "ImportError_",
    "Importer",
    "ParsedHolding",
    "ParsedLink",
    "ParsedStatement",
    "ParsedTransaction",
    "enricher_for",
    "module",
    "parse_german_amount",
    "pdf_first_page_text",
    "pdf_page_texts",
]


def pdf_page_texts(path: Path, x_tolerance: float = 3) -> list[str]:
    """The text of every page as pdfplumber extracts it (``x_tolerance``: how far apart
    characters may be and still form one word)."""
    import pdfplumber

    with pdfplumber.open(path) as pdf:
        return [page.extract_text(x_tolerance=x_tolerance) or "" for page in pdf.pages]


def pdf_first_page_text(path: Path, x_tolerance: float = 3) -> str:
    """The first page's text, for ``detect``; empty if the file is no readable PDF."""
    import pdfplumber

    try:
        with pdfplumber.open(path) as pdf:
            if not pdf.pages:
                return ""
            return pdf.pages[0].extract_text(x_tolerance=x_tolerance) or ""
    except Exception:  # not a readable PDF
        return ""


def module(plugin: str, name: str) -> types.ModuleType:
    """Module ``name`` of the available plugin ``plugin`` (a replaced plugin: the user's)."""
    from hiro_budget.importers.plugins import current

    return current().module(plugin, name)


def enricher_for(*, bank_code: str | None = None, institution: str | None = None):
    """The function ``(counterparty, purpose, booking_date) -> (merchant, purchase_date)``
    a plugin registered for this bank, or None."""
    from hiro_budget.importers.plugins import current

    return current().enricher_for(bank_code=bank_code, institution=institution)
