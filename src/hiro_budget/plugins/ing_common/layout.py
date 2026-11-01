"""Layout of ING (ING-DiBa) statements shared by the ING importers: text extraction
setting, the statement header (IBAN, number, date, balances, account, period)."""

from __future__ import annotations

import re
from datetime import date

from hiro_budget.plugin_api import parse_german_amount

# Character spacing tolerance: 1.5 keeps word gaps in the purpose text.
X_TOLERANCE = 1.5

DATE = r"\d{2}\.\d{2}\.\d{4}"
AMOUNT = r"-?\d{1,3}(?:\.\d{3})*,\d{2}"

PAGE_MARKER = re.compile(r"^T_\w+$")

HEADER_IBAN = re.compile(r"IBAN ((?:DE|AT|NL)\d{2}(?: ?\d{4}){4} ?\d{2})")
HEADER_STATEMENT_NO = re.compile(r"Auszugsnummer (\d+)")
HEADER_DATE = re.compile(rf"^Datum ({DATE})", re.MULTILINE)
HEADER_OPENING = re.compile(rf"Alter Saldo ({AMOUNT}) Euro")
HEADER_CLOSING = re.compile(rf"Neuer Saldo ({AMOUNT}) Euro")
HEADER_ACCOUNT = re.compile(r"^(.+?) Nummer (\d+)$", re.MULTILINE)
HEADER_PERIOD = re.compile(r"Kontoauszug (\w+) (\d{4})")
HEADER_YEAR = re.compile(r"^Kontoauszug (\d{4})$", re.MULTILINE)  # yearly (Extra-Konto)

GERMAN_MONTHS = {
    m: i
    for i, m in enumerate(
        [
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
        ],
        start=1,
    )
}

ACCOUNT_KINDS = {
    "Girokonto": "giro",
    "Extra-Konto": "savings",
    "Tagesgeld": "savings",
    "Sparplan": "savings",
}


def parse_date(text: str) -> date:
    """``31.12.2026`` as a date."""
    d, m, y = text.split(".")
    return date(int(y), int(m), int(d))


def _month_end(year: int, month: int) -> date:
    if month == 12:
        return date(year, 12, 31)
    return date.fromordinal(date(year, month + 1, 1).toordinal() - 1)


def parse_header(text: str) -> dict:
    def find(rx: re.Pattern[str]) -> str | None:
        m = rx.search(text)
        return m.group(1) if m else None

    header: dict = {}
    iban = find(HEADER_IBAN)
    header["account_iban"] = iban.replace(" ", "") if iban else None
    header["statement_no"] = find(HEADER_STATEMENT_NO)
    d = find(HEADER_DATE)
    header["statement_date"] = parse_date(d) if d else None
    opening, closing = find(HEADER_OPENING), find(HEADER_CLOSING)
    header["opening_balance_cents"] = parse_german_amount(opening) if opening else None
    header["closing_balance_cents"] = parse_german_amount(closing) if closing else None

    acc = HEADER_ACCOUNT.search(text)
    product = acc.group(1).strip() if acc else "Konto"
    number = acc.group(2) if acc else ""
    header["account_name"] = f"ING {product} {number}".strip()
    header["account_kind"] = ACCOUNT_KINDS.get(product, "giro")

    period = HEADER_PERIOD.search(text)
    if period and period.group(1) in GERMAN_MONTHS:
        month, year = GERMAN_MONTHS[period.group(1)], int(period.group(2))
        header["period_start"] = date(year, month, 1)
        header["period_end"] = _month_end(year, month)
    elif year_only := HEADER_YEAR.search(text):
        year = int(year_only.group(1))
        header["period_start"] = date(year, 1, 1)
        header["period_end"] = date(year, 12, 31)
    return header
