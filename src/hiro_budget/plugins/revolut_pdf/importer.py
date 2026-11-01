"""Importer for Revolut account statements (German PDF export, "EUR-Kontoauszug").

One export covers any date range and may contain several sections, one per Revolut
entity the account was held with (Revolut Payments UAB, Revolut Bank UAB, its German
branch). Each section starts with a balance summary and lists its bookings with the
running balance::

    Zusammenfassung der Bilanz
    Produkt Eröffnungssaldo Geldausgang Geldeingang Schlusssaldo
    Konto (Girokonto) 24,34€ 147,84€ 200,00€ 76,50€
    Kontotransaktionen von 19. August 2026 bis 21. Oktober 2026
    Datum Beschreibung Geldausgang Geldeingang Kontostand
    23.08.2026 Google Pay Einzahlung von *1234 100,00€ 124,34€
    Von: *5678
    14.09.2026 Cloud Storage Ltd 7,44€ 95,48€
    Gebühr: 0,07€ 7,37€
    Revolut Kurs: 1,00€ = 1,15$ (ECB-Kurs* 1,00€ = 1,16$) 8,50$
    An: Cloud Storage Ltd, example.com, CA
    Karte: 416598******0000

Out- and incoming amounts are distinguishable only by column position, so the sign is
taken from the change of the running balance. Reversed transactions ("Rückgängig
gemacht") are not part of the balance and are skipped, as is the move of the balance
from one Revolut entity to the next at a section boundary.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from hiro_budget.plugin_api import (
    ImportError_,
    ParsedStatement,
    ParsedTransaction,
    parse_german_amount,
    pdf_first_page_text,
    pdf_page_texts,
)

log = logging.getLogger(__name__)

X_TOLERANCE = 1.5

_DATE = r"\d{2}\.\d{2}\.\d{4}"
_AMOUNT = r"-?\d{1,3}(?:\.\d{3})*,\d{2}€"
MONTHS = {
    name: i
    for i, name in enumerate(
        (
            "Januar",
            "Februar",
            "März",
            "April",
            "Mai",
            "Juni",
            "Juli",
            "August",
            "September",
            "Oktober",
            "November",
            "Dezember",
        ),
        start=1,
    )
}
_LONG_DATE = r"\d{1,2}\. \w+ \d{4}"

TITLE = re.compile(r"^(\w{3})-Kontoauszug$", re.MULTILINE)
GENERATED = re.compile(rf"^Generiert am ({_DATE})$", re.MULTILINE)
IBAN = re.compile(r"\bIBAN ([A-Z]{2}\d{2}[A-Z0-9]+)")
SUMMARY_START = "Zusammenfassung der Bilanz"
SUMMARY = re.compile(rf"^Konto \(.+?\) ({_AMOUNT}) ({_AMOUNT}) ({_AMOUNT}) ({_AMOUNT})$")
PERIOD = re.compile(rf"^Kontotransaktionen von ({_LONG_DATE}) bis ({_LONG_DATE})$")
TABLE_HEADER = "Datum Beschreibung Geldausgang Geldeingang Kontostand"
TABLE_END = ("Verlorene oder gestohlene Karte melden", "Rückgängig gemacht")
BOOKING_LINE = re.compile(rf"^({_DATE}) (.+?) ({_AMOUNT}) ({_AMOUNT})$")
IBAN_ONLY = re.compile(r"^[A-Z]{2}\d{2}[A-Z0-9]{10,30}$")
DETAIL = re.compile(r"^(An|Von|Karte|Referenz|Gebühr|Revolut Kurs): ?(.*)$")


def _date(text: str) -> date:
    d, m, y = text.split(".")
    return date(int(y), int(m), int(d))


def _long_date(text: str) -> date:
    d, month, y = text.split()
    if month not in MONTHS:
        raise ImportError_(f"unknown month in '{text}'")
    return date(int(y), MONTHS[month], int(d.rstrip(".")))


def _amount(text: str) -> int:
    return parse_german_amount(text.removesuffix("€"))


@dataclass
class _Booking:
    day: date
    description: str
    amount_cents: int  # unsigned until the running balance gives the direction
    balance_cents: int
    lines: list[str]
    details: list[tuple[str, str]] = field(default_factory=list)
    extra: list[str] = field(default_factory=list)


@dataclass
class _Section:
    entity: str
    opening_cents: int
    out_cents: int
    in_cents: int
    closing_cents: int
    period_start: date | None = None
    period_end: date | None = None
    bookings: list[_Booking] = field(default_factory=list)


def _sections(pages: list[str]) -> list[_Section]:
    sections: list[_Section] = []
    for page in pages:
        lines = [line.strip() for line in page.splitlines() if line.strip()]
        if SUMMARY_START in lines:
            summary = next((m for line in lines if (m := SUMMARY.match(line))), None)
            if summary is None:
                raise ImportError_("balance summary not found")
            opening, out, in_, closing = (_amount(g) for g in summary.groups())
            # The page header names the Revolut entity: title, "Generiert am", entity.
            sections.append(_Section(lines[2], opening, out, in_, closing))
        if not sections:
            continue
        section = sections[-1]
        inside = False
        for line in lines:
            if m := PERIOD.match(line):
                section.period_start = _long_date(m.group(1))
                section.period_end = _long_date(m.group(2))
            if not inside:
                inside = line == TABLE_HEADER
                continue
            if line.startswith(TABLE_END):
                break
            if m := BOOKING_LINE.match(line):
                section.bookings.append(
                    _Booking(
                        day=_date(m.group(1)),
                        description=m.group(2),
                        amount_cents=_amount(m.group(3)),
                        balance_cents=_amount(m.group(4)),
                        lines=[line],
                    )
                )
            elif section.bookings:
                b = section.bookings[-1]
                b.lines.append(line)
                if m := DETAIL.match(line):
                    b.details.append((m.group(1), m.group(2)))
                elif not b.details and not any(c.isdigit() for c in line):
                    b.description += " " + line  # wrapped description
                else:
                    b.extra.append(line)  # e.g. the foreign amount of a refund
    return sections


def _to_transaction(b: _Booking, amount_cents: int) -> ParsedTransaction:
    details = dict(b.details)
    party = details.get("An") or details.get("Von") or ""
    t = ParsedTransaction(
        booking_date=b.day,
        value_date=b.day,
        amount_cents=amount_cents,
        booking_type="",
        purpose=" ".join([f"{k}: {v}" for k, v in b.details if k != "Karte"] + b.extra),
        raw_text="\n".join(b.lines),
    )
    if "Karte" in details:
        t.booking_type = "Kartenzahlung" if amount_cents < 0 else "Kartengutschrift"
        t.merchant = b.description
        t.counterparty = party
        t.card_purchase_date = b.day
        return t
    # Without a card the description says what happened ("Google Pay Einzahlung von *1234").
    t.purpose = f"{b.description} {t.purpose}".strip()
    if m := re.match(r"(?:Google Pay |Apple Pay )?Einzahlung von (.+)$", b.description):
        t.booking_type = "Einzahlung"
        t.counterparty = m.group(1)
    else:
        t.booking_type = "Überweisung" if amount_cents < 0 else "Gutschrift"
        # A bank transfer's "Von: NAME, IBAN" carries the complete name.
        sender = party.rsplit(", ", 1) if party else []
        if len(sender) == 2 and IBAN_ONLY.match(sender[1]):
            t.counterparty = sender[0]
        elif m := re.match(r"(?:Zahlung von|To) (.+)$", b.description):
            t.counterparty = m.group(1)
        else:
            t.counterparty = b.description
    return t


def _signed(section: _Section, warnings: list[str]) -> list[tuple[_Booking, int]]:
    result = []
    previous = section.opening_cents
    for b in section.bookings:
        change = b.balance_cents - previous
        if abs(change) != b.amount_cents:
            warnings.append(
                f"{b.day:%d.%m.%Y} {b.description}: amount {b.amount_cents / 100:.2f} does "
                f"not match the balance change {change / 100:.2f}"
            )
        result.append((b, b.amount_cents if change >= 0 else -b.amount_cents))
        previous = b.balance_cents
    out = -sum(a for _, a in result if a < 0)
    in_ = sum(a for _, a in result if a > 0)
    if (out, in_) != (section.out_cents, section.in_cents):
        warnings.append(
            f"{section.entity}: bookings out/in {out / 100:.2f}/{in_ / 100:.2f} do not match "
            f"the summary {section.out_cents / 100:.2f}/{section.in_cents / 100:.2f}"
        )
    return result


def parse_pages(pages: list[str]) -> ParsedStatement:
    text = "\n".join(pages)
    title = TITLE.search(text)
    if not title:
        raise ImportError_("not a Revolut account statement")
    if title.group(1) != "EUR":
        raise ImportError_(f"only EUR statements are supported, not {title.group(1)}")
    sections = _sections(pages)
    if not sections:
        raise ImportError_("no balance summary found")
    warnings: list[str] = []
    signed = [_signed(s, warnings) for s in sections]
    # Drop the pair of bookings that moves the balance to the next Revolut entity.
    for i in range(1, len(sections)):
        before, after = signed[i - 1], signed[i]
        # Each booking is named after the other entity; together they cancel out.
        if (
            before
            and after
            and sections[i].entity.startswith(before[-1][0].description)
            and sections[i - 1].entity.startswith(after[0][0].description)
            and before[-1][1] == -after[0][1]
        ):
            before.pop()
            after.pop(0)
    generated = GENERATED.search(text)
    iban = IBAN.search(text)
    stmt = ParsedStatement(
        importer=RevolutPdfImporter.name,
        institution="Revolut",
        account_name="Revolut",
        account_kind="giro",
        account_iban=iban.group(1) if iban else None,
        statement_date=_date(generated.group(1)) if generated else None,
        period_start=sections[0].period_start,
        period_end=sections[-1].period_end,
        opening_balance_cents=sections[0].opening_cents,
        closing_balance_cents=sections[-1].closing_cents,
        transactions=[_to_transaction(b, a) for part in signed for b, a in part],
        warnings=warnings,
    )
    if not stmt.balance_ok:
        stmt.warnings.append(
            f"balance mismatch: difference {stmt.balance_difference_cents / 100:.2f} EUR"
        )
    return stmt


def extract_pages(path: Path) -> list[str]:
    return pdf_page_texts(path, X_TOLERANCE)


class RevolutPdfImporter:
    name = "revolut_pdf"

    def detect(self, path: Path) -> bool:
        if path.suffix.lower() != ".pdf":
            return False
        first = pdf_first_page_text(path, X_TOLERANCE)
        return "Revolut" in first and bool(TITLE.search(first)) and SUMMARY_START in first

    def parse(self, path: Path) -> ParsedStatement:
        try:
            pages = extract_pages(path)
        except Exception as e:
            raise ImportError_(f"cannot read {path.name}: {e}") from e
        stmt = parse_pages(pages)
        for w in stmt.warnings:
            log.warning("%s: %s", path.name, w)
        return stmt
