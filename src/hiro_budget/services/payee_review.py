"""Working through uncategorized bookings by payee.

Uncategorized bookings are grouped by the payee a rule would match (the merchant of
card payments, else the counterparty). Each group gets the category this payee was
given most often before as a suggestion; a group is assigned in one step, and a rule
can catch the payee's future bookings.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, replace
from datetime import date

from sqlalchemy.orm import Session

from hiro_budget.config import RuleDef, RuleMatch
from hiro_budget.services import transactions as txs
from hiro_budget.services.transactions import TransactionRow, TxFilter


def rule_source(row: TransactionRow) -> tuple[str, str]:
    """(field, text) a rule for this booking's payee matches on."""
    if row.merchant and row.merchant.strip():
        return "merchant", row.merchant.strip()
    return "counterparty", (row.counterparty or "").strip()


def payee_key(row: TransactionRow) -> str:
    field, text = rule_source(row)
    return f"{field}:{text.casefold()}"


@dataclass
class PayeeGroup:
    payee: str  # as shown in the bookings list
    field: str  # "merchant" or "counterparty": what a rule matches on
    source: str  # the text a rule matches
    rows: list[TransactionRow]  # newest first
    suggestion: int | None = None  # category id this payee was given most often

    @property
    def count(self) -> int:
        return len(self.rows)

    @property
    def total_cents(self) -> int:
        return sum(r.amount_cents for r in self.rows)

    @property
    def first(self) -> date:
        return min(r.booking_date for r in self.rows)

    @property
    def last(self) -> date:
        return max(r.booking_date for r in self.rows)


def uncategorized_groups(session: Session, f: TxFilter | None = None) -> list[PayeeGroup]:
    """Uncategorized bookings (within the filter's dates, account and text) by payee,
    the largest groups first."""
    base = f or TxFilter()
    rows = txs.list_transactions(session, replace(base, category=txs.UNCATEGORIZED))
    groups: dict[str, PayeeGroup] = {}
    for r in rows:
        key = payee_key(r)
        if key not in groups:
            field, source = rule_source(r)
            groups[key] = PayeeGroup(r.payee or source, field, source, [])
        groups[key].rows.append(r)
    suggestions = _suggestions(session, set(groups))
    for key, g in groups.items():
        g.suggestion = suggestions.get(key)
    return sorted(groups.values(), key=lambda g: (-g.count, -abs(g.total_cents), g.payee))


def _suggestions(session: Session, keys: set[str]) -> dict[str, int]:
    """Most frequent category of confirmed (rule or manual) bookings per payee."""
    counts: dict[str, Counter[int]] = {}
    for r in txs.list_transactions(session, TxFilter()):
        if r.is_uncategorized or r.needs_review or r.single_category_id is None:
            continue
        key = payee_key(r)
        if key in keys:
            counts.setdefault(key, Counter())[r.single_category_id] += 1
    return {key: c.most_common(1)[0][0] for key, c in counts.items()}


def assign(
    session: Session,
    tx_ids: list[int],
    *,
    category_id: int | None = None,
    transfer_account_id: int | None = None,
    days: int = txs.DEFAULT_MATCH_DAYS,
) -> None:
    """Give all bookings the category (or mark them as transfers to the account)."""
    for tx_id in tx_ids:
        if transfer_account_id is not None:
            txs.mark_transfer(session, tx_id, transfer_account_id, days)
        else:
            txs.set_category(session, tx_id, category_id, days)


def payee_rule(group: PayeeGroup, category_key: str, rule_id: str) -> RuleDef:
    """A rule giving this payee's bookings the category (case-insensitive, literal)."""
    pattern = "(?i)" + re.escape(group.source)
    return RuleDef(id=rule_id, category=category_key, match=RuleMatch(**{group.field: pattern}))
