"""Importer for ING mortgage loan statements ("Baufinanzierung Kontoauszug").

Layout (one value date per booking, continuation lines without date)::

    Ihre Baufinanzierung Kontonummer 9988776655
    Kontoauszug 2025
    Valuta Vorgang Euro
    Saldovortrag -200.000,00
    30.05.2025 Zinsen -500,00
    30.05.2025 Lastschrifteinzug Max Muster 1.500,00
    ING-DIBA AG 9988776655 30.05.2025 ZINSEN 500,00 TILGUNG
    1.000,00
    ...
    Neuer Saldo -199.000,00

The payments are already booked on the paying account (split into interest and a
principal transfer to the loan), so the loan's own bookings would count them twice.
The statement is therefore imported as a balance statement (``valuation``): its closing
balance anchors the loan balance; the bookings only serve the balance check and preview.
"""

from __future__ import annotations

import logging
import re
from datetime import date
from pathlib import Path

from hiro_budget.plugin_api import (
    ImportError_,
    ParsedStatement,
    ParsedTransaction,
    module,
    parse_german_amount,
    pdf_first_page_text,
    pdf_page_texts,
)

layout = module("ing_common", "layout")
HEADER_DATE, HEADER_IBAN, PAGE_MARKER = layout.HEADER_DATE, layout.HEADER_IBAN, layout.PAGE_MARKER
X_TOLERANCE, parse_header = layout.X_TOLERANCE, layout.parse_header

log = logging.getLogger(__name__)

_DATE = r"\d{2}\.\d{2}\.\d{4}"
_AMOUNT = r"-?\d{1,3}(?:\.\d{3})*,\d{2}"

ACCOUNT = re.compile(r"Baufinanzierung Kontonummer (\d+)")
REGION_START = re.compile(r"^Valuta Vorgang Euro$")
REGION_END = re.compile(rf"^Neuer Saldo {_AMOUNT}$")
OPENING = re.compile(rf"^Saldovortrag ({_AMOUNT})$", re.MULTILINE)
CLOSING = re.compile(rf"^Neuer Saldo ({_AMOUNT})(?: Euro)?$", re.MULTILINE)
BOOKING_LINE = re.compile(rf"^({_DATE}) (.+?) ({_AMOUNT})[1-9]?$")


def _date(text: str) -> date:
    d, m, y = text.split(".")
    return date(int(y), int(m), int(d))


def booking_lines(pages: list[str]) -> list[str]:
    lines: list[str] = []
    for page in pages:
        inside = False
        for line in page.splitlines():
            line = line.strip()
            if not line:
                continue
            if not inside:
                inside = bool(REGION_START.match(line))
                continue
            if PAGE_MARKER.match(line) or REGION_END.match(line):
                break
            lines.append(line)
    return lines


def parse_bookings(lines: list[str]) -> list[ParsedTransaction]:
    transactions: list[ParsedTransaction] = []
    raw: list[str] = []
    purpose: list[str] = []

    def close() -> None:
        if transactions:
            transactions[-1].purpose = " ".join(purpose)
            transactions[-1].raw_text = "\n".join(raw)

    for line in lines:
        if OPENING.match(line):
            continue
        m = BOOKING_LINE.match(line)
        if m:
            close()
            booking_type, _, counterparty = m.group(2).partition(" ")
            transactions.append(
                ParsedTransaction(
                    booking_date=_date(m.group(1)),
                    value_date=_date(m.group(1)),
                    amount_cents=parse_german_amount(m.group(3)),
                    booking_type=booking_type,
                    counterparty=counterparty,
                )
            )
            raw, purpose = [line], []
        elif transactions:
            raw.append(line)
            purpose.append(line)
    close()
    return transactions


def parse_pages(pages: list[str]) -> ParsedStatement:
    text = "\n".join(pages)
    number = ACCOUNT.search(text)
    if not number:
        raise ImportError_("no loan account number found")
    header = parse_header(text)  # period ("Kontoauszug 2025" or month), IBAN, date
    opening = OPENING.search(text)
    closing = CLOSING.search(text)
    iban = HEADER_IBAN.search(text)
    statement_date = HEADER_DATE.search(text)
    stmt = ParsedStatement(
        importer=IngLoanPdfImporter.name,
        institution="ING",
        account_name=f"ING Baufinanzierung {number.group(1)}",
        account_kind="loan",
        account_iban=iban.group(1).replace(" ", "") if iban else None,
        statement_date=_date(statement_date.group(1)) if statement_date else None,
        period_start=header.get("period_start"),
        period_end=header.get("period_end"),
        opening_balance_cents=parse_german_amount(opening.group(1)) if opening else None,
        closing_balance_cents=parse_german_amount(closing.group(1)) if closing else None,
        transactions=parse_bookings(booking_lines(pages)),
        valuation=True,
    )
    if stmt.period_end is None:
        stmt.warnings.append("statement period not found")
    if not stmt.balance_checkable:
        stmt.warnings.append("opening/closing balance not found - balance cannot be verified")
    elif not stmt.balance_ok:
        stmt.warnings.append(
            f"balance mismatch: difference {stmt.balance_difference_cents / 100:.2f} EUR"
        )
    return stmt


def extract_pages(path: Path) -> list[str]:
    return pdf_page_texts(path, X_TOLERANCE)


class IngLoanPdfImporter:
    name = "ing_loan_pdf"

    def detect(self, path: Path) -> bool:
        if path.suffix.lower() != ".pdf":
            return False
        first = pdf_first_page_text(path, X_TOLERANCE)
        return "ING-DiBa" in first and bool(ACCOUNT.search(first))

    def parse(self, path: Path) -> ParsedStatement:
        try:
            pages = extract_pages(path)
        except Exception as e:
            raise ImportError_(f"cannot read {path.name}: {e}") from e
        stmt = parse_pages(pages)
        for w in stmt.warnings:
            log.warning("%s: %s", path.name, w)
        return stmt
