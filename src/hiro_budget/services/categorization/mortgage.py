"""Split mortgage payments into interest and principal."""

from __future__ import annotations

import re
from dataclasses import dataclass

from hiro_budget.db.models import Account, Category, Transaction
from hiro_budget.money import parse_german_amount
from hiro_budget.services.categorization.transfers import PatternMatcher

_AMOUNT = r"\d{1,3}(?:\.\d{3})*,\d{2}"
INTEREST_PRINCIPAL = re.compile(rf"ZINSEN\s+({_AMOUNT})\s+TILGUNG\s+({_AMOUNT})", re.IGNORECASE)


@dataclass
class MortgageSplit:
    interest_cents: int  # signed like the booking (negative for a payment)
    principal_cents: int


def extract_split(purpose: str, amount_cents: int) -> MortgageSplit | None:
    """Parse ``ZINSEN x TILGUNG y`` from the purpose; parts must add up to the amount."""
    m = INTEREST_PRINCIPAL.search(purpose)
    if not m:
        return None
    sign = -1 if amount_cents < 0 else 1
    interest = sign * parse_german_amount(m.group(1))
    principal = sign * parse_german_amount(m.group(2))
    if interest + principal != amount_cents:
        return None
    return MortgageSplit(interest, principal)


@dataclass
class MortgageMatcher:
    name: str
    matcher: PatternMatcher
    loan_account: Account
    interest_category: Category
    principal_category: Category | None = None

    def applies(self, tx: Transaction) -> bool:
        return self.matcher.matches(tx)
