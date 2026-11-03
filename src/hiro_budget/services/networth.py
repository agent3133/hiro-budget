"""Net worth over time: the balance of every account at each month end.

The balance of an account on a day follows the same starting points as the account
balances (see ``accounts.account_anchor``):

- Securities accounts and loans (valuations): the latest valuation up to that day plus
  the bookings after it; before the first valuation it is worked back from that
  valuation with the bookings in between (a loan: its principal payments), as far back
  as the account has bookings.
- Other accounts: the anchor balance plus the bookings since; before the anchor the
  balance is unknown, except for cash, which counts as 0 before it was tracked.

Before its first known balance an account counts with that balance (estimated, see
``AccountHistory.estimated``), so that the total does not jump when an account's history
begins and one account without history (e.g. one added with only an opening balance) does
not cut the report short. ``balances`` keep None for the unknown months (the mortgage
report reads them). The report starts in the first month any account has a balance.
"""

from __future__ import annotations

import bisect
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from hiro_budget.db.models import Account, AccountKind, ImportBatch, Transaction
from hiro_budget.services.accounts import account_anchor
from hiro_budget.services.reports import months_between


def month_end(month: str) -> date:
    y, m = map(int, month.split("-"))
    first_next = date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1)
    return first_next - timedelta(days=1)


@dataclass
class AccountHistory:
    account_id: int
    name: str
    kind: AccountKind
    balances: dict[str, int | None] = field(default_factory=dict)  # month -> cents
    has_bookings: bool = True

    def first_known(self) -> str | None:
        return next((m for m, v in sorted(self.balances.items()) if v is not None), None)

    def estimated(self, month: str) -> bool:
        """Whether the month counts with the first known balance (carried back)."""
        first = self.first_known()
        return self.balances.get(month) is None and first is not None and month < first

    def value(self, month: str) -> int:
        """The balance as counted in the totals: the first known one before it."""
        cents = self.balances.get(month)
        first = self.first_known()
        if cents is None and first is not None and self.estimated(month):
            cents = self.balances[first]
        return cents or 0


@dataclass
class NetWorth:
    months: list[str]
    accounts: list[AccountHistory]
    # first month every account with bookings has a known balance (cash aside): income
    # and expenses are complete from there (savings rate, year comparison)
    complete_from: str | None = None

    def _values(self, month: str) -> list[int]:
        return [a.value(month) for a in self.accounts]

    def estimated(self, months: list[str]) -> list[tuple[AccountHistory, str]]:
        """Accounts estimated in some of ``months``, with their first known month."""
        return [
            (a, a.first_known() or "") for a in self.accounts if any(a.estimated(m) for m in months)
        ]

    def assets(self, month: str) -> int:
        return sum(v for v in self._values(month) if v > 0)

    def debts(self, month: str) -> int:
        """Negative balances (loans, overdrafts) as a negative number."""
        return sum(v for v in self._values(month) if v < 0)

    def net(self, month: str) -> int:
        return sum(self._values(month))


class _Balances:
    """Balance of one account on any day."""

    def __init__(self, account: Account, batches: list[ImportBatch], bookings) -> None:
        self.kind = account.kind
        self.dates = [d for d, _ in bookings]
        self.cumulative: list[int] = []
        total = 0
        for _, cents in bookings:
            total += cents
            self.cumulative.append(total)
        self.valuations = sorted(
            (b.period_end, b.closing_balance_cents)
            for b in batches
            if b.valuation and b.period_end is not None and b.closing_balance_cents is not None
        )
        self.anchor = account_anchor(account, batches)

    def _sum_until(self, day: date) -> int:
        """Bookings up to and including ``day``."""
        i = bisect.bisect_right(self.dates, day)
        return self.cumulative[i - 1] if i else 0

    def on(self, day: date) -> int | None:
        if self.valuations:
            ends = [d for d, _ in self.valuations]
            i = bisect.bisect_right(ends, day)
            if i:  # latest valuation up to the day plus the bookings after it
                end, value = self.valuations[i - 1]
                return value + self._sum_until(day) - self._sum_until(end)
            end, value = self.valuations[0]  # back from the first valuation
            if not self.dates or day < self.dates[0] - timedelta(days=1):
                return None  # no bookings that far back
            return value - (self._sum_until(end) - self._sum_until(day))
        kind, start, base = self.anchor
        if start is None:  # no starting point: all bookings count
            return self._sum_until(day)
        if day < start - timedelta(days=1):
            return 0 if self.kind == AccountKind.CASH else None
        return base + self._sum_until(day) - self._sum_until(start - timedelta(days=1))


def net_worth(session: Session, last_month: str, account_id: int | None = None) -> NetWorth:
    """Month-end balances from the first month all accounts have data up to ``last_month``
    (at most the current month)."""
    last_month = min(last_month, f"{date.today():%Y-%m}")
    accounts = list(session.scalars(select(Account).order_by(Account.name)))
    if account_id is not None:
        accounts = [a for a in accounts if a.id == account_id]
    batches: dict[int, list[ImportBatch]] = defaultdict(list)
    for b in session.scalars(select(ImportBatch)):
        batches[b.account_id].append(b)
    bookings: dict[int, list[tuple[date, int]]] = defaultdict(list)
    for acc_id, day, cents in session.execute(
        select(Transaction.account_id, Transaction.booking_date, Transaction.amount_cents).order_by(
            Transaction.booking_date
        )
    ):
        bookings[acc_id].append((day, cents))

    first_dates = [d for rows in bookings.values() for d, _ in rows[:1]]
    first_dates += [b.period_end for rows in batches.values() for b in rows if b.period_end]
    if not accounts or not first_dates:
        return NetWorth([], [])
    first_month = f"{min(first_dates):%Y-%m}"
    months = months_between(first_month, last_month)

    histories = []
    for acc in accounts:
        balances = _Balances(acc, batches[acc.id], bookings[acc.id])
        history = AccountHistory(acc.id, acc.name, acc.kind, has_bookings=bool(bookings[acc.id]))
        for m in months:
            history.balances[m] = balances.on(month_end(m))
        histories.append(history)

    known = [s for h in histories if (s := h.first_known()) is not None]
    start = min(known) if known else first_month
    months = [m for m in months if m >= start]
    for h in histories:
        h.balances = {m: h.balances[m] for m in months}
    # an account without bookings has no income or expenses that could be missing
    complete = [
        first
        for h in histories
        if h.kind != AccountKind.CASH and h.has_bookings and (first := h.first_known()) is not None
    ]
    return NetWorth(months, histories, max(complete) if complete else start)


def running_balances(session: Session, account_id: int) -> dict[int, int | None]:
    """The account's balance after each of its bookings (by booking id), as on a bank
    statement: the balance at the end of the booking day (the same as the net worth and
    the accounts page use) minus the later bookings of that day, in booking order (date,
    id). None where the balance is not known (before the first statement)."""
    account = session.get(Account, account_id)
    if account is None:
        return {}
    batches = list(session.scalars(select(ImportBatch).where(ImportBatch.account_id == account_id)))
    rows = session.execute(
        select(Transaction.id, Transaction.booking_date, Transaction.amount_cents)
        .where(Transaction.account_id == account_id)
        .order_by(Transaction.booking_date, Transaction.id)
    ).all()
    balances = _Balances(account, batches, [(day, cents) for _, day, cents in rows])
    result: dict[int, int | None] = {}
    i = len(rows)
    while i:  # day by day from the newest, each day from its last booking back
        day = rows[i - 1][1]
        running = balances.on(day)
        while i and rows[i - 1][1] == day:
            tx_id, _, cents = rows[i - 1]
            result[tx_id] = running
            if running is not None:
                running -= cents
            i -= 1
    return result
