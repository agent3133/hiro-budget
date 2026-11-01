"""Importer for ING (ING-DiBa) account statement PDFs ("Kontoauszug").

Layout of the booking table (one booking = two or more lines)::

    01.09.2026 Lastschrift VISA SUPERMARKT -57,40         <- date, type, counterparty, amount
    01.09.2026 NR XXXX 1234 MUSTERSTADT DE KAUFUMSATZ 29.08 ...  <- value date, purpose
    ARN00000000000000000000001 Google Pay                <- continuation lines
    Mandat:123456 / Referenz:...                         <- references

Each page's booking region starts after the ``Valuta`` column header and ends at the
page marker line (``T_...``) or at ``Neuer Saldo`` on the last page. The statement
header (IBAN, balances, ...) is read from the whole text with regular expressions
(``ing_common.layout``).
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
card_texts = module("ing_common", "card_texts")

log = logging.getLogger(__name__)

X_TOLERANCE = layout.X_TOLERANCE
_DATE, _AMOUNT = layout.DATE, layout.AMOUNT
PAGE_MARKER = layout.PAGE_MARKER
enrich = card_texts.enrich
_date = layout.parse_date

# The amount may carry a superscript footnote marker that text extraction appends as a
# digit ("-1.234,561" = -1.234,56 with footnote 1, e.g. for a deviating value date).
BOOKING_LINE = re.compile(rf"^({_DATE}) (.+?) ({_AMOUNT})[1-9]?$")
VALUTA_LINE = re.compile(rf"^({_DATE})(?: (.*))?$")
REGION_START = re.compile(r"^Valuta$")
REGION_END = re.compile(rf"^Neuer Saldo {_AMOUNT}")
REFERENCE_LINE = re.compile(r"^(Mandat|Referenz):\s*(.*)$")

# Known booking types, matched longest first against the start of the booking line.
BOOKING_TYPES = sorted(
    [
        "Lastschrift",
        "Lastschrift-Einzug",
        "Gutschrift",
        "Gutschrift/Dauerauftrag",
        "Gehalt/Rente",
        "Bezuege",
        "girocard",
        "Echtzeitüberweisung",
        "Überweisung",
        "Ueberweisung",
        "Dauerauftrag/Terminueberw.",
        "Dauerauftrag",
        "Entgelt",
        "Abbuchung",
        "Abschluss",
        "Zinsgutschrift",
        "Zinsertrag",
        "Zins/Dividende",
        "Zins/Dividende WP",
        "Retoure",
        "Rücklastschrift",
        "Storno",
        "Bargeldauszahlung",
        "Bargeldeinzahlung",
        "Kapitalertragsteuer",
        "Solidaritätszuschlag",
        "Kirchensteuer",
        "Wertpapierkauf",
        "Wertpapierverkauf",
        "Wertpapiergutschrift",
    ],
    key=len,
    reverse=True,
)


def split_type_and_counterparty(text: str) -> tuple[str, str, bool]:
    """Split ``"Lastschrift VISA ALDI SUED"`` into type and counterparty.

    Returns ``(type, counterparty, known)``; unknown types fall back to the first word.
    """
    for t in BOOKING_TYPES:
        if text == t or text.startswith(t + " "):
            return t, text[len(t) :].strip(), True
    first, _, rest = text.partition(" ")
    return first, rest.strip(), False


class _TxBuilder:
    def __init__(self, booking_date: date, rest: str, amount: str, raw: str) -> None:
        self.booking_type, self.counterparty, self.known_type = split_type_and_counterparty(rest)
        self.booking_date = booking_date
        self.amount_cents = parse_german_amount(amount)
        self.value_date: date | None = None
        self.purpose_parts: list[str] = []
        self.mandate_ref: str | None = None
        self.reference: str | None = None
        self.raw_lines = [raw]

    def add_line(self, line: str) -> None:
        self.raw_lines.append(line)
        if self.value_date is None and len(self.raw_lines) == 2:
            m = VALUTA_LINE.match(line)
            if m:
                self.value_date = _date(m.group(1))
                if m.group(2):
                    self.purpose_parts.append(m.group(2))
                return
        ref = REFERENCE_LINE.match(line)
        if ref:
            if ref.group(1) == "Mandat":
                self.mandate_ref = ref.group(2).strip()
            else:
                self.reference = ref.group(2).strip()
            return
        self.purpose_parts.append(line)

    def build(self) -> ParsedTransaction:
        purpose = " ".join(self.purpose_parts)
        merchant, purchase_date = enrich(self.counterparty, purpose, self.booking_date)
        return ParsedTransaction(
            booking_date=self.booking_date,
            value_date=self.value_date,
            amount_cents=self.amount_cents,
            booking_type=self.booking_type,
            counterparty=self.counterparty,
            purpose=purpose,
            mandate_ref=self.mandate_ref,
            reference=self.reference,
            merchant=merchant,
            card_purchase_date=purchase_date,
            raw_text="\n".join(self.raw_lines),
        )


def booking_lines(pages: list[str]) -> list[str]:
    """Return the lines of all booking regions, in order."""
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


def parse_bookings(lines: list[str]) -> tuple[list[ParsedTransaction], list[str]]:
    transactions: list[ParsedTransaction] = []
    warnings: list[str] = []
    current: _TxBuilder | None = None
    for line in lines:
        m = BOOKING_LINE.match(line)
        # A booking line has a date, text and an amount; a valuta line never ends in an amount
        # preceded by text that starts with a booking type, but to be safe we only start a new
        # booking when the previous one already has its valuta line.
        if m and (current is None or current.value_date is not None):
            if current:
                transactions.append(current.build())
            current = _TxBuilder(_date(m.group(1)), m.group(2), m.group(3), line)
            if not current.known_type:
                warnings.append(
                    f"unknown booking type '{current.booking_type}' on {m.group(1)}: {line}"
                )
            continue
        if current is None:
            warnings.append(f"ignored line before first booking: {line}")
            continue
        current.add_line(line)
    if current:
        transactions.append(current.build())
    return transactions, warnings


def parse_pages(pages: list[str]) -> ParsedStatement:
    """Parse the extracted text of all pages into a statement (pure function, testable)."""
    full = "\n".join(pages)
    header = layout.parse_header(full)
    transactions, warnings = parse_bookings(booking_lines(pages))
    stmt = ParsedStatement(
        importer=IngGiroPdfImporter.name,
        institution="ING",
        transactions=transactions,
        warnings=warnings,
        **header,
    )
    if not stmt.account_iban:
        stmt.warnings.append("no IBAN found in statement header")
    if not stmt.balance_checkable:
        stmt.warnings.append("opening/closing balance not found - balance cannot be verified")
    elif not stmt.balance_ok:
        stmt.warnings.append(
            f"balance mismatch: difference {stmt.balance_difference_cents / 100:.2f} EUR"
        )
    return stmt


def extract_pages(path: Path) -> list[str]:
    return pdf_page_texts(path, X_TOLERANCE)


class IngGiroPdfImporter:
    name = "ing_pdf"

    def detect(self, path: Path) -> bool:
        if path.suffix.lower() != ".pdf":
            return False
        first = pdf_first_page_text(path, X_TOLERANCE)
        return (
            ("ING-DiBa" in first or "INGDDEFFXXX" in first)
            and "Kontoauszug" in first
            and "Depotauszug" not in first
            and "Baufinanzierung Kontonummer" not in first  # see ing_loan_pdf
        )

    def parse(self, path: Path) -> ParsedStatement:
        try:
            pages = extract_pages(path)
        except Exception as e:
            raise ImportError_(f"cannot read {path.name}: {e}") from e
        stmt = parse_pages(pages)
        if not stmt.transactions:
            raise ImportError_(f"{path.name}: no bookings found")
        for w in stmt.warnings:
            log.warning("%s: %s", path.name, w)
        return stmt
