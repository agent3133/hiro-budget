"""Budget proposals derived from past spending.

For each budget category the spending (outflows minus refunds) of the full months before
the budget month is looked at:

- regular categories (spending in at least two thirds of the considered months, e.g.
  groceries): median of the last ``months`` months, robust against one-off purchases;
- irregular categories (insurance, gifts, repairs): total of the last 12 months spread
  over 12 months as monthly provision (YNAB "true expenses"). With less than 12 months of
  history the total is divided by the available months, which errs on the safe side.

Groups budgeted as a whole get one proposal from the summed spending of their categories.
Proposals are rounded up to a multiple of ``round_to`` euros.
"""

from __future__ import annotations

import math
import statistics
from collections import defaultdict
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from hiro_budget.config import Settings
from hiro_budget.db.models import Account, Category, CategoryGroup, Split, Transaction
from hiro_budget.services.budget import (
    Envelope,
    get_assigned,
    month_add,
    month_first_day,
    month_of,
    months_between,
    set_assigned,
)

REGULAR_SHARE = 2 / 3
IRREGULAR_WINDOW = 12


@dataclass
class Proposal:
    category_id: int | None  # None for a group budgeted as a whole
    key: str
    name: str
    group_name: str
    regular: bool
    group_id: int = 0
    is_group: bool = False
    spending: list[int] = field(default_factory=list)  # per month, oldest first (cents)
    months_with_spending: int = 0
    proposal: int = 0  # cents, rounded
    assigned: int = 0  # currently assigned in the budget month

    @property
    def envelope(self) -> Envelope:
        if self.is_group:
            return Envelope("group", self.group_id)
        return Envelope("category", self.category_id)

    @property
    def minimum(self) -> int:
        return min(self.spending, default=0)

    @property
    def maximum(self) -> int:
        return max(self.spending, default=0)

    @property
    def median(self) -> int:
        return round(statistics.median(self.spending)) if self.spending else 0

    @property
    def average(self) -> int:
        return round(sum(self.spending) / len(self.spending)) if self.spending else 0


@dataclass
class ProposalResult:
    month: str
    months: int  # months considered for regular categories
    history_from: str | None  # first month with data in the considered window
    history_to: str | None
    history_months: int  # months of history available (up to 12)
    proposals: list[Proposal] = field(default_factory=list)

    @property
    def total(self) -> int:
        return sum(p.proposal for p in self.proposals)


def round_up(cents: int, round_to_euros: int) -> int:
    if cents <= 0:
        return 0
    if round_to_euros <= 0:
        return cents
    step = round_to_euros * 100
    return math.ceil(cents / step) * step


def _first_month(session: Session) -> str | None:
    first = session.scalar(
        select(func.min(Transaction.booking_date))
        .join(Account, Transaction.account_id == Account.id)
        .where(Account.on_budget.is_(True))
    )
    return month_of(first) if first else None


def propose(
    session: Session,
    settings: Settings,
    month: str,
    months: int | None = None,
    round_to: int | None = None,
) -> ProposalResult:
    months = months or settings.app.budget.proposal_months
    round_to = settings.app.budget.proposal_round_to if round_to is None else round_to
    last = month_add(month, -1)  # the budget month itself is not complete yet
    first_data = _first_month(session)
    window_start = month_add(month, -IRREGULAR_WINDOW)
    if first_data is None or months_between(first_data, last) < 0:
        return ProposalResult(month, months, None, None, 0)
    if months_between(window_start, first_data) > 0:
        window_start = first_data
    history = [month_add(window_start, i) for i in range(months_between(window_start, last) + 1)]

    spending: dict[int, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    rows = session.execute(
        select(Split.category_id, Transaction.booking_date, Split.amount_cents)
        .join(Transaction, Split.transaction_id == Transaction.id)
        .join(Account, Transaction.account_id == Account.id)
        .join(Category, Split.category_id == Category.id)
        .join(CategoryGroup, Category.group_id == CategoryGroup.id)
        .where(
            Account.on_budget.is_(True),
            CategoryGroup.is_income.is_(False),
            Transaction.booking_date >= month_first_day(window_start),
            Transaction.booking_date < month_first_day(month),
        )
    )
    for cat_id, booked, amount in rows:
        spending[cat_id][month_of(booked)] -= amount  # outflows count positive

    recent = history[-months:]
    result = ProposalResult(
        month=month,
        months=len(recent),
        history_from=history[0],
        history_to=history[-1],
        history_months=len(history),
    )
    groups = session.scalars(
        select(CategoryGroup)
        .where(CategoryGroup.is_income.is_(False), CategoryGroup.hidden.is_(False))
        .order_by(CategoryGroup.sort, CategoryGroup.id)
        .options(selectinload(CategoryGroup.categories))
    ).all()
    for g in groups:
        if g.budget_by_group:
            per_month: dict[str, int] = defaultdict(int)
            for c in g.categories:  # hidden ones too: their past spending counts
                for m, cents in spending.get(c.id, {}).items():
                    per_month[m] += cents
            line = Proposal(None, g.key, g.name, g.name, False, g.id, True)
            result.proposals.append(
                _fill(session, line, month, per_month, recent, history, round_to)
            )
            continue
        for c in g.categories:
            if c.hidden:
                continue
            line = Proposal(c.id, c.key, c.name, g.name, False, g.id)
            result.proposals.append(
                _fill(session, line, month, spending.get(c.id, {}), recent, history, round_to)
            )
    return result


def _fill(
    session: Session,
    p: Proposal,
    month: str,
    per_month: dict[str, int],
    recent: list[str],
    history: list[str],
    round_to: int,
) -> Proposal:
    series_recent = [per_month.get(m, 0) for m in recent]
    series_all = [per_month.get(m, 0) for m in history]
    with_spending = sum(1 for v in series_recent if v > 0)
    p.regular = with_spending >= math.ceil(len(recent) * REGULAR_SHARE) and with_spending > 0
    if p.regular:
        raw = round(statistics.median(series_recent))
        p.spending = series_recent
        p.months_with_spending = with_spending
    else:
        raw = round(sum(series_all) / max(len(history), 1))
        p.spending = series_all
        p.months_with_spending = sum(1 for v in series_all if v > 0)
    p.proposal = round_up(raw, round_to)
    p.assigned = get_assigned(session, month, p.envelope)
    return p


def apply_proposals(session: Session, month: str, amounts: dict[int | Envelope, int]) -> int:
    """Set Assigned of the given envelopes (category id or Envelope -> cents); returns count."""
    for target, cents in amounts.items():
        set_assigned(session, month, target, cents)
    return len(amounts)
