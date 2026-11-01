"""The start page in numbers: what needs attention now, and how things are going.

Everything comes from the other services (budget, net worth, savings, recurring costs,
statement gaps); this module only picks what the start page shows.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy.orm import Session

from hiro_budget.config import Settings
from hiro_budget.services import budget, networth, savings
from hiro_budget.services import transactions as txs
from hiro_budget.services.accounts import StatementGap, account_balances
from hiro_budget.services.recurring import Recurring, recurring_costs

UPCOMING_DAYS = 30  # fixed costs due within this many days
TREND_MONTHS = 12  # net worth: this many months back


@dataclass
class Overview:
    month: str  # "YYYY-MM", the current month
    ready_to_assign: int
    overspent: list[tuple[str, int]]  # category name, available (negative), worst first
    underfunded: int  # categories whose target is not funded yet
    uncategorized: int  # all bookings without a category
    to_review: int  # suggestions of the classifier
    income: int  # this month
    expenses: int
    net_worth: list[tuple[str, int]]  # month, cents (up to TREND_MONTHS + 1 months)
    upcoming: list[Recurring] = field(default_factory=list)  # by due date
    gaps: list[tuple[str, StatementGap]] = field(default_factory=list)  # account, gap

    @property
    def saved(self) -> int:
        return self.income - self.expenses

    @property
    def net_worth_now(self) -> int | None:
        return self.net_worth[-1][1] if self.net_worth else None

    @property
    def net_worth_change(self) -> int | None:
        """Change over the shown months (None with less than two months)."""
        if len(self.net_worth) < 2:
            return None
        return self.net_worth[-1][1] - self.net_worth[0][1]


def overview(session: Session, settings: Settings, today: date | None = None) -> Overview:
    today = today or date.today()
    month = f"{today:%Y-%m}"
    b = budget.compute_month(session, settings, month)
    overspent = sorted(
        ((c.name, c.available) for c in b.envelopes if c.overspent), key=lambda x: x[1]
    )
    underfunded = sum(1 for c in b.envelopes if c.underfunded and not c.overspent)

    first_day = today.replace(day=1)
    rates = savings.savings_rates(session, first_day, today)
    this = next((m for m in rates.months if m.month == month), None)

    worth = networth.net_worth(session, month)
    months = worth.months[-(TREND_MONTHS + 1) :]
    trend = [(m, worth.net(m)) for m in months]

    costs = recurring_costs(session, today)
    soon = today + timedelta(days=UPCOMING_DAYS)
    upcoming = sorted((r for r in costs.items if r.next_due <= soon), key=lambda r: r.next_due)

    gaps = [(a.name, g) for a in account_balances(session) for g in a.gaps]
    return Overview(
        month=month,
        ready_to_assign=b.ready_to_assign,
        overspent=overspent,
        underfunded=underfunded,
        uncategorized=txs.count_transactions(session, txs.TxFilter(category=txs.UNCATEGORIZED)),
        to_review=txs.review_count(session),
        income=this.income_cents if this else 0,
        expenses=this.expense_cents if this else 0,
        net_worth=trend,
        upcoming=upcoming,
        gaps=gaps,
    )
