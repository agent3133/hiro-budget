"""Detection of transfers between own accounts."""

from __future__ import annotations

import re
from dataclasses import dataclass

from hiro_budget.config import PatternMatch
from hiro_budget.db.models import Account, Category, Transaction


@dataclass
class PatternMatcher:
    """Matches a booking against optional counterparty/purpose regexes (all must match)."""

    counterparty: re.Pattern[str] | None
    purpose: re.Pattern[str] | None

    @classmethod
    def compile(cls, m: PatternMatch) -> PatternMatcher:
        return cls(
            counterparty=re.compile(m.counterparty) if m.counterparty else None,
            purpose=re.compile(m.purpose) if m.purpose else None,
        )

    @property
    def empty(self) -> bool:
        return self.counterparty is None and self.purpose is None

    def matches(self, tx: Transaction) -> bool:
        if self.empty:
            return False
        if self.counterparty and not self.counterparty.search(tx.counterparty or ""):
            return False
        return not (self.purpose and not self.purpose.search(tx.purpose or ""))


@dataclass
class TransferTarget:
    account: Account
    matcher: PatternMatcher
    # Budget category for outflows from a budget account into this tracking account.
    category: Category | None = None


def find_transfer_target(targets: list[TransferTarget], tx: Transaction) -> TransferTarget | None:
    """Return the own account this booking transfers money to/from."""
    for t in targets:
        if t.account is not tx.account and t.matcher.matches(tx):
            return t
    return None
