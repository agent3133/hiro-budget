"""ING's card and PayPal booking texts: merchant name and card purchase date.

Registered as the enricher of ING (bank code 50010517): FinTS and CSV formats use it for
ING bookings, the ING importers call it directly.
"""

from __future__ import annotations

import re
from datetime import date

CARD_PURCHASE = re.compile(r"KAUFUMSATZ ?(\d{2})\.(\d{2})")
CARD_PREFIX = re.compile(r"^VISA ")
PAYMENT_PROVIDER_PREFIX = re.compile(r"^(?:PAYPAL|SUMUP|SPC|SQ)\s*\*\s*", re.IGNORECASE)
# Text extraction occasionally inserts stray spaces inside words ("Ihr Ei nkauf bei").
PAYPAL_SHOP = re.compile(r"I\s*h\s*r\s+E\s*i\s*n\s*k\s*a\s*u\s*f\s+b\s*e\s*i\s+([^,]+?)\s*(?:,|$)")


def enrich(counterparty: str, purpose: str, booking_date: date) -> tuple[str | None, date | None]:
    """Derive a clean merchant name and the card purchase date where possible."""
    merchant: str | None = None
    purchase: date | None = None
    if CARD_PREFIX.match(counterparty):
        merchant = PAYMENT_PROVIDER_PREFIX.sub("", CARD_PREFIX.sub("", counterparty)).strip()
        m = CARD_PURCHASE.search(purpose)
        if m:
            day, month = int(m.group(1)), int(m.group(2))
            year = booking_date.year - (1 if month > booking_date.month else 0)
            try:
                purchase = date(year, month, day)
            except ValueError:
                purchase = None
    elif counterparty.lower().startswith("paypal"):
        m = PAYPAL_SHOP.search(purpose)
        merchant = f"PayPal: {m.group(1).strip()}" if m else "PayPal"
    return merchant, purchase
