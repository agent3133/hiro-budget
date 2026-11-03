"""The start page in numbers: what needs attention now, and how things are going.

Everything comes from the other services (budget, net worth, savings, recurring costs,
statement gaps); this module only picks what the start page shows.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy.orm import Session

from hiro_budget.config import Settings
from hiro_budget.db.models import AccountKind
from hiro_budget.services import budget, networth, savings
from hiro_budget.services import transactions as txs
from hiro_budget.services.accounts import AccountBalance, StatementGap, account_balances
from hiro_budget.services.recurring import Recurring, recurring_costs

UPCOMING_DAYS = 30  # fixed costs due within this many days
TREND_MONTHS = 12  # net worth: this many months back
#: days without a booking (or statement) after which an account gets a note, by kind;
#: loans are left out (their instalments are booked on the paying account)
STALE_DAYS = {
    AccountKind.GIRO: 35,
    AccountKind.SAVINGS: 35,
    AccountKind.CREDIT_CARD: 35,
    AccountKind.WALLET: 35,
    AccountKind.CASH: 42,
    AccountKind.DEPOT: 92,
}


@dataclass
class StaleAccount:
    """An account without a booking (or statement) for longer than its limit."""

    account_id: int
    name: str
    last: date  # last booking or end of the last statement
    days: int  # since then


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
    stale: list[StaleAccount] = field(default_factory=list)  # longest silence first

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

    balances = account_balances(session)
    gaps = [(a.name, g) for a in balances for g in a.gaps]
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
        stale=stale_accounts(settings, balances, today),
    )


def stale_accounts(
    settings: Settings, balances: list[AccountBalance], today: date
) -> list[StaleAccount]:
    """Own accounts whose last booking (or statement end) is older than the limit of their
    kind, unless their reminder is off (``stale_hint: false``); accounts never used are
    left out."""
    quiet = {a.name for a in settings.app.own_accounts if not a.stale_hint}
    result = []
    for b in balances:
        limit = STALE_DAYS.get(b.kind)
        dates = [d for d in (b.last_booking, b.statement_date) if d is not None]
        if limit is None or b.name in quiet or not dates:
            continue
        last = max(dates)
        if (today - last).days > limit:
            result.append(StaleAccount(b.account_id, b.name, last, (today - last).days))
    return sorted(result, key=lambda a: -a.days)
