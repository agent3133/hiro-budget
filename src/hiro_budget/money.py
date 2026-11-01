"""Money helpers. Amounts are stored as integer cents everywhere."""

from __future__ import annotations

import re
from decimal import Decimal

_GERMAN_AMOUNT = re.compile(r"^(-)?(\d{1,3}(?:\.\d{3})*|\d+),(\d{2})$")
_USER_GERMAN = re.compile(r"^(\d{1,3}(\.\d{3})+|\d+)(,\d{1,2})?$|^\d{1,3}(\.\d{3})+$")
_USER_PLAIN = re.compile(r"^\d+(\.\d{1,2})?$")


def parse_german_amount(text: str) -> int:
    """Parse a German formatted amount like ``-1.234,56`` into cents (-123456)."""
    m = _GERMAN_AMOUNT.match(text.strip())
    if not m:
        raise ValueError(f"not a German amount: {text!r}")
    sign, euros, cents = m.groups()
    value = int(euros.replace(".", "")) * 100 + int(cents)
    return -value if sign else value


def euros_to_cents(value: float) -> int:
    return round(value * 100)


def format_cents(cents: int, *, currency: str | None = "€", locale: str = "de") -> str:
    """Format cents for display, e.g. ``-1.234,56 €`` (de) or ``-1,234.56 €`` (en)."""
    sign = "-" if cents < 0 else ""
    euros, rest = divmod(abs(cents), 100)
    grouped = f"{euros:,}"
    if locale == "de":
        text = f"{sign}{grouped.replace(',', '.')},{rest:02d}"
    else:
        text = f"{sign}{grouped}.{rest:02d}"
    return f"{text} {currency}" if currency else text


def parse_user_amount(text: str) -> int:
    """Parse an amount typed by the user into cents.

    Accepts German ("1.234,56", "12,5") and plain ("1234.56") notation, an optional
    "€" and spaces. Raises ValueError for anything else.
    """
    t = text.replace("€", "").replace(" ", "").replace(" ", "").strip()
    if not t:
        raise ValueError("empty amount")
    sign = -1 if t.startswith("-") else 1
    t = t[1:] if t[:1] in "+-" else t
    if _USER_GERMAN.match(t):  # 1.234,56 / 1234,5 / 1.234
        t = t.replace(".", "").replace(",", ".")
    elif not _USER_PLAIN.match(t):  # 1234.56 / 1234
        raise ValueError(f"not an amount: {text!r}")
    return sign * round(Decimal(t) * 100)


def apply_amount_input(text: str, current_cents: int) -> int:
    """Budget input: "+50"/"-20" change the current value, anything else replaces it.

    A leading "=" forces an absolute value (e.g. "=-20").
    """
    t = text.strip()
    if t.startswith("="):
        return parse_user_amount(t[1:])
    if t.startswith(("+", "-")):
        return current_cents + parse_user_amount(t)
    return parse_user_amount(t)
