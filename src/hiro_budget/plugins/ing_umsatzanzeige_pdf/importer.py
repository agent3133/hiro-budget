"""Importer for ING "Umsatzanzeige" PDFs (booking list exported from online banking).

Unlike the Kontoauszug it covers any period (e.g. the current month before its statement
exists), lists the newest booking first and has no opening/closing balance block; each
booking carries the balance after it instead::

    Buchung Auftraggeber/Empfänger Saldo Betrag
    Wertstellun Buchungstext
    gsdatum Verwendungszweck
    Notiz
    21.10.2026 VISA SUPERMARKT MUSTERSTADT 1.234,56 € -1,35 €
    21.10.2026 Lastschrift
    NR XXXX 1234 MUSTERSTADT DEKAUFUMSATZ19.10
    1.35000000ARN00000000000000000000002Google Pay
    ...
    1/7

The opening balance is derived from the oldest booking; every booking is checked against
the running balance. The purposes lack the Mandat/Referenz lines of the Kontoauszug, so
importing the Kontoauszug later with "overwrite" replaces these bookings with richer ones.
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
ACCOUNT_KINDS, X_TOLERANCE = layout.ACCOUNT_KINDS, layout.X_TOLERANCE
enrich = card_texts.enrich

log = logging.getLogger(__name__)

_DATE = r"\d{2}\.\d{2}\.\d{4}"
_AMOUNT = r"\d{1,3}(?:\.\d{3})*,\d{2}"

TITLE = "Umsatzanzeige"
TABLE_HEADER = "Buchung Auftraggeber/Empfänger Saldo Betrag"
REGION_START = "gsdatum Verwendungszweck"  # last line of the table header
NOTE_HEADER = "Notiz"  # optional further header line
PAGE_NUMBER = re.compile(r"^\d+/\d+$")
# The payee is missing for e.g. securities purchases and dividends.
BOOKING_LINE = re.compile(rf"^({_DATE}) (?:(.+?) )?(-?{_AMOUNT}) € ([+-]{_AMOUNT}) €$")
VALUTA_LINE = re.compile(rf"^({_DATE}) (.+)$")
IBAN = re.compile(r"^((?:DE|AT|NL)\d{2}(?: \d{4}){4} \d{2})$", re.MULTILINE)
ACCOUNT_NAME = re.compile(r"^Kontoname (.+)$", re.MULTILINE)
CREATED = re.compile(rf"({_DATE}), \d{{2}}:\d{{2}} Uhr")
PERIOD = re.compile(rf"^({_DATE}) bis ({_DATE}) .*Datum (auf|ab)steigend (\d+)$", re.MULTILINE)

# Booking texts of the Umsatzanzeige that the Kontoauszug spells differently.
BOOKING_TYPES = {
    "Dauerauftrag / Terminueberweisung": "Dauerauftrag/Terminueberw.",
    "Gutschrift aus Dauerauftrag": "Gutschrift/Dauerauftrag",
    "Gutschrift Echtzeitüberweisung": "Gutschrift",
    "Überweisung": "Ueberweisung",
    "Zins / Dividende WP": "Zins/Dividende WP",
}


def _date(text: str) -> date:
    d, m, y = text.split(".")
    return date(int(y), int(m), int(d))


def booking_lines(pages: list[str]) -> list[str]:
    lines: list[str] = []
    for page in pages:
        page_lines = [line.strip() for line in page.splitlines() if line.strip()]
        # The page number ("1/7") ends the page; a purpose line may look the same.
        if page_lines and PAGE_NUMBER.match(page_lines[-1]):
            page_lines.pop()
        if REGION_START in page_lines:
            region = page_lines[page_lines.index(REGION_START) + 1 :]
            if region and region[0] == NOTE_HEADER:
                region.pop(0)
            lines.extend(region)
    return lines


class _Booking:
    def __init__(self, m: re.Match[str]) -> None:
        self.booking_date = _date(m.group(1))
        self.counterparty = m.group(2) or ""
        self.balance_cents = parse_german_amount(m.group(3))
        self.amount_cents = parse_german_amount(m.group(4).lstrip("+"))
        self.value_date: date | None = None
        self.booking_type = ""
        self.purpose: list[str] = []
        self.raw = [m.group(0)]

    def build(self) -> ParsedTransaction:
        purpose = " ".join(self.purpose)
        merchant, purchase_date = enrich(self.counterparty, purpose, self.booking_date)
        return ParsedTransaction(
            booking_date=self.booking_date,
            value_date=self.value_date,
            amount_cents=self.amount_cents,
            booking_type=BOOKING_TYPES.get(self.booking_type, self.booking_type),
            counterparty=self.counterparty,
            purpose=purpose,
            merchant=merchant,
            card_purchase_date=purchase_date,
            raw_text="\n".join(self.raw),
        )


def parse_bookings(lines: list[str]) -> list[_Booking]:
    bookings: list[_Booking] = []
    for line in lines:
        m = BOOKING_LINE.match(line)
        if m:
            bookings.append(_Booking(m))
            continue
        if not bookings:
            continue
        b = bookings[-1]
        b.raw.append(line)
        valuta = VALUTA_LINE.match(line)
        if b.value_date is None and valuta:
            b.value_date = _date(valuta.group(1))
            b.booking_type = valuta.group(2)
        else:
            b.purpose.append(line)
    return bookings


def parse_pages(pages: list[str]) -> ParsedStatement:
    text = "\n".join(pages)
    iban = IBAN.search(text)
    if not iban:
        raise ImportError_("no IBAN found")
    name = ACCOUNT_NAME.search(text)
    product = name.group(1).strip() if name else "Girokonto"
    created = CREATED.search(text)
    period = PERIOD.search(text)
    bookings = parse_bookings(booking_lines(pages))
    if not bookings:
        raise ImportError_("no bookings found")
    warnings: list[str] = []
    if period and period.group(3) == "ab":
        bookings.reverse()  # oldest first, as in a Kontoauszug
    if period and int(period.group(4)) != len(bookings):
        warnings.append(f"{len(bookings)} bookings found, the header lists {period.group(4)}")
    for b in bookings:
        if b.value_date is None:
            warnings.append(f"{b.booking_date:%d.%m.%Y} {b.counterparty}: no booking text")
    for before, b in zip(bookings, bookings[1:], strict=False):
        if before.balance_cents + b.amount_cents != b.balance_cents:
            warnings.append(
                f"{b.booking_date:%d.%m.%Y} {b.counterparty}: amount {b.amount_cents / 100:.2f}"
                " does not match the balance change"
            )
    first, last = bookings[0], bookings[-1]
    stmt = ParsedStatement(
        importer=IngUmsatzanzeigePdfImporter.name,
        institution="ING",
        account_name=f"ING {product}",
        account_kind=ACCOUNT_KINDS.get(product, "giro"),
        account_iban=iban.group(1).replace(" ", ""),
        statement_date=_date(created.group(1)) if created else None,
        period_start=_date(period.group(1)) if period else first.booking_date,
        period_end=_date(period.group(2)) if period else last.booking_date,
        opening_balance_cents=first.balance_cents - first.amount_cents,
        closing_balance_cents=last.balance_cents,
        transactions=[b.build() for b in bookings],
        warnings=warnings,
    )
    if not stmt.balance_ok:
        stmt.warnings.append(
            f"balance mismatch: difference {stmt.balance_difference_cents / 100:.2f} EUR"
        )
    return stmt


def extract_pages(path: Path) -> list[str]:
    return pdf_page_texts(path, X_TOLERANCE)


class IngUmsatzanzeigePdfImporter:
    name = "ing_umsatzanzeige_pdf"

    def detect(self, path: Path) -> bool:
        if path.suffix.lower() != ".pdf":
            return False
        first = pdf_first_page_text(path, X_TOLERANCE)
        lines = first.splitlines()
        return bool(lines) and lines[0].strip() == TITLE and TABLE_HEADER in first

    def parse(self, path: Path) -> ParsedStatement:
        try:
            pages = extract_pages(path)
        except Exception as e:
            raise ImportError_(f"cannot read {path.name}: {e}") from e
        stmt = parse_pages(pages)
        for w in stmt.warnings:
            log.warning("%s: %s", path.name, w)
        return stmt
