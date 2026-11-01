"""Importer for ING securities account statements ("Depotauszug").

A Depotauszug lists the holdings on a date, not bookings::

    Direkt-Depot Nr.: 8015716694
    Datum: 12.10.2026
    Depotauszug per 30.09.2026
    ...
    385 Stück HSBC EURO STOXX 50 UCITS ETF 65,80 EUR 25.333,00 EUR   <- position line
    Registered Shares o.N.
    ISIN (WKN): IE00B4K6B022 (A0YF4H)
    ...
    Anzahl Posten: 2 Gesamtkurswert 121.168,63 EUR

The result is a valuation statement: holdings plus their total value as closing balance.
"""

from __future__ import annotations

import logging
import re
from datetime import date
from pathlib import Path

from hiro_budget.plugin_api import (
    ImportError_,
    ParsedHolding,
    ParsedStatement,
    module,
    parse_german_amount,
    pdf_first_page_text,
    pdf_page_texts,
)

X_TOLERANCE = module("ing_common", "layout").X_TOLERANCE

log = logging.getLogger(__name__)

_DATE = r"\d{2}\.\d{2}\.\d{4}"
_AMOUNT = r"\d{1,3}(?:\.\d{3})*,\d{2}"
_NUMBER = r"\d{1,3}(?:\.\d{3})*(?:,\d+)?"

DEPOT_NO = re.compile(r"Depot Nr\.: (\d+)")
PRODUCT = re.compile(r"^(.*?Depot) Nr\.:", re.MULTILINE)
STATEMENT_DATE = re.compile(rf"^Datum: ({_DATE})", re.MULTILINE)
# "Depotauszug per 30.09.2026"; yearly: "Jahresdepotauszug ... zum 31.12.2025 abgeschlossene"
AS_OF = re.compile(rf"Depotauszug per ({_DATE})|zum ({_DATE}) abgeschlossene")
POSITION = re.compile(
    rf"^(?P<qty>{_NUMBER}) (?P<unit>Stück|Nominale) (?P<name>.+?) "
    rf"(?P<price>{_NUMBER}) (?P<pcur>[A-Z]{{3}}|%) (?P<value>{_AMOUNT}) EUR$"
)
ISIN_WKN = re.compile(r"^ISIN \(WKN\): ([A-Z]{2}[A-Z0-9]{9}\d)(?: \((\w+)\))?")
TOTAL = re.compile(rf"Anzahl Posten: (\d+) Gesamtkurswert ({_AMOUNT}) EUR")


def _date(text: str) -> date:
    d, m, y = text.split(".")
    return date(int(y), int(m), int(d))


def _number(text: str) -> float:
    return float(text.replace(".", "").replace(",", "."))


def parse_holdings(text: str) -> list[ParsedHolding]:
    holdings: list[ParsedHolding] = []
    for line in text.splitlines():
        line = line.strip()
        m = POSITION.match(line)
        if m:
            holdings.append(
                ParsedHolding(
                    name=m.group("name"),
                    quantity=_number(m.group("qty")),
                    unit=m.group("unit"),
                    value_cents=parse_german_amount(m.group("value")),
                    price=_number(m.group("price")),
                    price_currency=m.group("pcur"),
                )
            )
            continue
        ids = ISIN_WKN.match(line)
        if ids and holdings and holdings[-1].isin is None:
            holdings[-1].isin, holdings[-1].wkn = ids.group(1), ids.group(2)
    return holdings


def parse_pages(pages: list[str]) -> ParsedStatement:
    """Parse the extracted text of all pages into a valuation statement."""
    text = "\n".join(pages)
    number = DEPOT_NO.search(text)
    if not number:
        raise ImportError_("no depot number found")
    product = PRODUCT.search(text)
    as_of = AS_OF.search(text)
    statement_date = STATEMENT_DATE.search(text)
    total = TOTAL.search(text)
    holdings = parse_holdings(text)
    period_end = _date(as_of.group(1) or as_of.group(2)) if as_of else None
    stmt = ParsedStatement(
        importer=IngDepotPdfImporter.name,
        institution="ING",
        account_name=f"ING {product.group(1) if product else 'Depot'} {number.group(1)}",
        account_kind="depot",
        # The depot number identifies the account like an IBAN does for bank accounts.
        account_iban=number.group(1),
        statement_date=_date(statement_date.group(1)) if statement_date else None,
        period_start=period_end,
        period_end=period_end,
        closing_balance_cents=parse_german_amount(total.group(2)) if total else None,
        valuation=True,
        holdings=holdings,
    )
    if period_end is None:
        stmt.warnings.append("valuation date ('Depotauszug per') not found")
    if total is None:
        stmt.warnings.append("total value not found - holdings cannot be verified")
    else:
        if int(total.group(1)) != len(holdings):
            stmt.warnings.append(
                f"{total.group(1)} positions announced, {len(holdings)} recognized"
            )
        if not stmt.balance_ok:
            stmt.warnings.append(
                f"holdings do not add up to the total value: difference "
                f"{stmt.balance_difference_cents / 100:.2f} EUR"
            )
    for h in holdings:
        if h.isin is None:
            stmt.warnings.append(f"no ISIN found for '{h.name}'")
    return stmt


def extract_pages(path: Path) -> list[str]:
    return pdf_page_texts(path, X_TOLERANCE)


class IngDepotPdfImporter:
    name = "ing_depot_pdf"

    def detect(self, path: Path) -> bool:
        if path.suffix.lower() != ".pdf":
            return False
        first = pdf_first_page_text(path, X_TOLERANCE)
        return "ING-DiBa" in first and "depotauszug" in first.lower()

    def parse(self, path: Path) -> ParsedStatement:
        try:
            pages = extract_pages(path)
        except Exception as e:
            raise ImportError_(f"cannot read {path.name}: {e}") from e
        stmt = parse_pages(pages)
        for w in stmt.warnings:
            log.warning("%s: %s", path.name, w)
        return stmt
