"""Fixed costs and subscriptions: outgoing payments that recur at a regular rhythm.

Outgoing bookings of the last ``LOOKBACK_MONTHS`` are grouped by payee (letters only, so
card and reference numbers do not split a payee) - not by direct debit mandate, which
not every import provides - and within a payee by similar amount (e.g. electricity and
gas from one supplier, or two subscriptions billed through PayPal). A group
is recurring when the gaps between its payments fit a rhythm and its amounts are
similar; it is still running when the last payment is not overdue by more than half a
rhythm. Transfers to own budget accounts (top-ups) are no costs; payments to tracking
accounts (loan instalments, savings plans) are.

Variable costs are the other expenses (as in the reports) of the last ``VARIABLE_MONTHS``
full months, averaged per month.
"""

from __future__ import annotations

import re
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from hiro_budget.db.models import Account, Category, CategoryGroup, Split, Transaction
from hiro_budget.services.reports import (
    is_income,
    months_between,
    report_date,
    report_splits,
)

LOOKBACK_MONTHS = 15
VARIABLE_MONTHS = 12
AMOUNT_TOLERANCE = 0.1  # amounts within 10 % belong to the same payment (price changes)
# rhythm: (name, typical days, allowed gap range in days)
RHYTHMS = [
    ("monthly", 30.44, (25, 36)),
    ("quarterly", 91.3, (80, 102)),
    ("half-yearly", 182.6, (165, 200)),
    ("yearly", 365.25, (340, 390)),
]
_NOT_LETTERS = re.compile(r"[^a-zäöüß]+")


def payee_key(name: str) -> str:
    """'NETFLIX.COM 866-579' and 'Netflix.com 1234' -> 'netflix com'."""
    return " ".join(_NOT_LETTERS.sub(" ", name.lower()).split())


@dataclass
class Payment:
    day: date
    cents: int  # positive = paid
    category: str | None
    payee: str
    card: bool = False  # card purchase (merchant), not a debit or transfer
    group_id: int | None = None  # the category's group
    transaction_id: int | None = None


@dataclass
class Recurring:
    payee: str
    category: str | None  # most frequent category name
    rhythm: str  # see RHYTHMS
    interval_days: float
    amount_cents: int  # typical amount (median of the last payments)
    last: date
    count: int
    payments: list[Payment] = field(default_factory=list)
    group_id: int | None = None  # group of ``category``

    @property
    def monthly_cents(self) -> int:
        """Cost per month (e.g. a yearly payment / 12)."""
        return round(self.amount_cents * 30.44 / self.interval_days)

    @property
    def next_due(self) -> date:
        return self.last + timedelta(days=round(self.interval_days))


@dataclass
class RecurringCosts:
    items: list[Recurring]  # still running, largest monthly cost first
    ended: list[Recurring]  # recurring, but the last payment is overdue

    @property
    def monthly_cents(self) -> int:
        return sum(r.monthly_cents for r in self.items)


def _clusters(payments: list[Payment]) -> list[list[Payment]]:
    """Split one payee's payments into groups of similar amounts."""
    groups: list[list[Payment]] = []
    for p in sorted(payments, key=lambda p: p.cents):
        if groups and p.cents <= groups[-1][0].cents * (1 + AMOUNT_TOLERANCE):
            groups[-1].append(p)
        else:
            groups.append([p])
    return groups


def _recurring(payments: list[Payment]) -> Recurring | None:
    payments = sorted(payments, key=lambda p: p.day)
    gaps = [(b.day - a.day).days for a, b in zip(payments, payments[1:], strict=False)]
    if not gaps:
        return None
    gap = statistics.median(gaps)
    rhythm = next(((n, d, r) for n, d, r in RHYTHMS if r[0] <= gap <= r[1]), None)
    if rhythm is None:
        return None
    name, days, (low, high) = rhythm
    fitting = sum(1 for g in gaps if low <= g <= high)
    if fitting < len(gaps) * 2 / 3:
        return None
    if len(payments) == 2:
        # One gap only (yearly, half-yearly): two shop visits a year apart are no
        # subscription - require a debit or transfer of (almost) the same amount.
        if name not in ("yearly", "half-yearly") or any(p.card for p in payments):
            return None
        a, b = (p.cents for p in payments)
        if abs(a - b) > max(a, b) * 0.02:
            return None
    recent = [p.cents for p in payments[-3:]]
    categories = Counter(p.category for p in payments if p.category)
    category = categories.most_common(1)[0][0] if categories else None
    names = Counter(p.payee for p in payments)
    return Recurring(
        payee=names.most_common(1)[0][0],
        category=category,
        rhythm=name,
        interval_days=days,
        amount_cents=round(statistics.median(recent)),
        last=payments[-1].day,
        count=len(payments),
        payments=payments,
        group_id=next((p.group_id for p in payments if p.category == category), None),
    )


def recurring_costs(
    session: Session, until: date, account_id: int | None = None, since: date | None = None
) -> RecurringCosts:
    """Recurring payments in the ``LOOKBACK_MONTHS`` up to ``until`` (from ``since`` when
    that is earlier)."""
    lookback = until - timedelta(days=round(LOOKBACK_MONTHS * 30.44))
    since = min(since, lookback) if since else lookback
    budget_accounts = set(session.scalars(select(Account.id).where(Account.on_budget.is_(True))))
    categories = {
        c.id: (c.name, c.group_id)
        for c in session.execute(select(Category.id, Category.name, Category.group_id))
    }
    # plain columns, not ORM entities: ten years of expenses in a fraction of the time (#60)
    q = select(
        Transaction.id,
        Transaction.booking_date,
        Transaction.amount_cents,
        Transaction.merchant,
        Transaction.counterparty,
        Transaction.booking_type,
    ).where(
        Transaction.amount_cents < 0,
        Transaction.counterpart_of_id.is_(None),
        Transaction.booking_date > since,
        Transaction.booking_date <= until,
    )
    if account_id is not None:
        q = q.where(Transaction.account_id == account_id)
    rows = session.execute(q).all()
    splits: dict[int, list[tuple[int | None, int | None]]] = defaultdict(list)
    ids = q.with_only_columns(Transaction.id).scalar_subquery()
    for tx_id, target, category_id in session.execute(
        select(Split.transaction_id, Split.transfer_account_id, Split.category_id)
        .where(Split.transaction_id.in_(ids))
        .order_by(Split.id)
    ):
        splits[tx_id].append((target, category_id))
    groups: dict[str, list[Payment]] = defaultdict(list)
    for tx_id, booking_date, amount, merchant, counterparty, booking_type in rows:
        parts = splits.get(tx_id, [])
        if parts and all(target in budget_accounts for target, _ in parts):
            continue  # money moved to another own budget account (e.g. a top-up)
        payee = merchant or counterparty or booking_type
        key = payee_key(payee)
        if not key:
            continue
        category_id = next((c for _, c in parts if c is not None), None)
        name, group_id = categories[category_id] if category_id is not None else (None, None)
        payment = Payment(
            booking_date,
            -amount,
            name,
            payee,
            card=merchant is not None,
            group_id=group_id,
            transaction_id=tx_id,
        )
        groups[key].append(payment)

    running: list[Recurring] = []
    ended: list[Recurring] = []
    for payments in groups.values():
        for cluster in _clusters(payments):
            item = _recurring(cluster)
            if item is None:
                continue
            overdue = (until - item.last).days > item.interval_days * 1.5
            (ended if overdue else running).append(item)
    running.sort(key=lambda r: r.monthly_cents, reverse=True)
    ended.sort(key=lambda r: r.last, reverse=True)
    return RecurringCosts(running, ended)


@dataclass
class VariableCosts:
    """The expenses that are no fixed costs, in the last full months."""

    first_month: str  # "YYYY-MM"; later than the window's start when the data starts later
    last_month: str
    expense_cents: int  # all expenses of these months
    fixed_cents: int  # of them, on bookings recognised as fixed costs

    @property
    def months(self) -> int:
        return len(months_between(self.first_month, self.last_month))

    @property
    def monthly_cents(self) -> int:
        return round((self.expense_cents - self.fixed_cents) / self.months)


def variable_costs(
    session: Session,
    costs: RecurringCosts,
    until: date,
    account_id: int | None = None,
    one_offs: bool = True,
) -> VariableCosts | None:
    """Expenses of the ``VARIABLE_MONTHS`` full months up to ``until`` without the
    bookings of ``costs`` (running and ended); None without expenses."""
    last = until if (until + timedelta(days=1)).day == 1 else until.replace(day=1) - timedelta(1)
    year, month = divmod(last.year * 12 + last.month - VARIABLE_MONTHS, 12)
    first = date(year, month + 1, 1)
    months = _expenses(session, costs, first, last, account_id, one_offs)
    if not months:
        return None
    return VariableCosts(
        min(months),
        last.strftime("%Y-%m"),
        sum(m.expense_cents for m in months.values()),
        sum(m.fixed_cents for m in months.values()),
    )


@dataclass
class MonthCosts:
    month: str  # "YYYY-MM", the reporting month
    expense_cents: int = 0  # all expenses, as in the reports
    fixed_cents: int = 0  # of them, on bookings recognised as fixed costs

    @property
    def variable_cents(self) -> int:
        return self.expense_cents - self.fixed_cents


def _expenses(
    session: Session,
    costs: RecurringCosts,
    first: date,
    last: date,
    account_id: int | None,
    one_offs: bool,
) -> dict[str, MonthCosts]:
    """Expenses per reporting month (only months that have any), and how much of them is
    on the bookings of ``costs`` (running and ended)."""
    fixed_ids = {p.transaction_id for r in costs.items + costs.ended for p in r.payments}
    q = report_splits(
        func.strftime("%Y-%m", report_date()),
        Split.transaction_id,
        Split.amount_cents,
        Split.category_id,
        CategoryGroup.is_income,
        Category.one_off,
        date_from=first,
        date_to=last,
        account_id=account_id,
    )
    months: dict[str, MonthCosts] = {}
    for month, tx_id, amount, category_id, income_group, one_off in session.execute(q):
        if is_income(amount, category_id, income_group) or (one_off and not one_offs):
            continue
        m = months.setdefault(month, MonthCosts(month))
        m.expense_cents -= amount
        if tx_id in fixed_ids:
            m.fixed_cents -= amount
    return months


def costs_by_month(
    session: Session,
    date_from: date,
    date_to: date,
    account_id: int | None = None,
    one_offs: bool = True,
) -> dict[str, MonthCosts]:
    """Fixed and variable costs per reporting month of a period; the fixed costs are
    detected over the whole period (at least ``LOOKBACK_MONTHS``)."""
    costs = recurring_costs(session, date_to, account_id, since=date_from)
    return _expenses(session, costs, date_from, date_to, account_id, one_offs)
