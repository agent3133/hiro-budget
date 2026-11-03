"""Aggregations for reports. Transfers between own accounts are always excluded.

Income = splits in income-group categories plus uncategorized inflows.
Expenses = all other categorized splits plus uncategorized outflows, reported as a
positive number (refunds in an expense category reduce its expenses).

Reports count a booking in its reporting month (``report_date``): the booking date, or
for a category with ``report_shift_days`` N, N days earlier, so that bookings on day <= N
count for the previous month. Splits of one-off categories can be left out
(``one_offs=False``); their sums are reported either way.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import Date, case, func, select, type_coerce
from sqlalchemy.orm import Session

from hiro_budget.config import MAX_REPORT_SHIFT_DAYS
from hiro_budget.db.models import Category, CategoryGroup, Split, Transaction

UNCATEGORIZED_KEY = "__uncategorized__"


@dataclass
class MonthTotals:
    month: str  # "YYYY-MM"
    income_cents: int = 0
    expense_cents: int = 0
    # of one-off categories; included in income/expense only with one_offs=True
    one_off_income_cents: int = 0
    one_off_expense_cents: int = 0

    @property
    def net_cents(self) -> int:
        return self.income_cents - self.expense_cents


@dataclass
class CategoryTotal:
    category_id: int | None
    key: str
    group_name: str
    category_name: str
    expense_cents: int  # positive = money spent
    one_off: bool = False
    group_id: int | None = None


def report_date():
    """The date a split counts at in reports (needs ``Category`` joined): the booking date,
    moved back by the category's ``report_shift_days`` (day <= N -> previous month)."""
    shift = func.coalesce(Category.report_shift_days, 0)
    shifted = func.date(Transaction.booking_date, func.printf("-%d days", shift))
    return type_coerce(case((shift > 0, shifted), else_=Transaction.booking_date), Date)


def _period(q, date_from: date | None, date_to: date | None, account_id: int | None):
    # The booking-date conditions only narrow the search (they can use its index): a
    # reporting date lies at most MAX_REPORT_SHIFT_DAYS before the booking date.
    if date_from:
        q = q.where(Transaction.booking_date >= date_from, report_date() >= date_from)
    if date_to:
        latest = date_to + timedelta(days=MAX_REPORT_SHIFT_DAYS)
        q = q.where(Transaction.booking_date <= latest, report_date() <= date_to)
    if account_id:
        q = q.where(Transaction.account_id == account_id)
    return q


def report_splits(*columns, date_from: date | None, date_to: date | None, account_id: int | None):
    """The splits all reports are made of (no transfers between own accounts), in the
    period by reporting date; ``columns`` may use Split, Transaction, Category and
    CategoryGroup."""
    q = (
        select(*columns)
        .join(Transaction, Split.transaction_id == Transaction.id)
        .outerjoin(Category, Split.category_id == Category.id)
        .outerjoin(CategoryGroup, Category.group_id == CategoryGroup.id)
        .where(Split.transfer_account_id.is_(None))
    )
    return _period(q, date_from, date_to, account_id)


def _base_query(date_from: date | None, date_to: date | None, account_id: int | None):
    return report_splits(
        func.strftime("%Y-%m", report_date()).label("month"),
        Split.amount_cents,
        Split.category_id,
        Category.key,
        Category.name,
        CategoryGroup.name,
        CategoryGroup.is_income,
        Category.one_off,
        Category.group_id,
        date_from=date_from,
        date_to=date_to,
        account_id=account_id,
    )


def is_income(amount: int, category_id: int | None, is_income_group: bool | None) -> bool:
    """Whether a split counts as income (else as expense) in the reports."""
    if category_id is None:
        return amount > 0
    return bool(is_income_group)


def months_between(start: str, end: str) -> list[str]:
    y, m = map(int, start.split("-"))
    ey, em = map(int, end.split("-"))
    out = []
    while (y, m) <= (ey, em):
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def monthly_totals(
    session: Session,
    date_from: date | None = None,
    date_to: date | None = None,
    account_id: int | None = None,
    one_offs: bool = True,
) -> list[MonthTotals]:
    totals: dict[str, MonthTotals] = {}
    for month, amount, cat_id, _key, _name, _group, income_group, one_off, _ in session.execute(
        _base_query(date_from, date_to, account_id)
    ):
        t = totals.setdefault(month, MonthTotals(month))
        income = is_income(amount, cat_id, income_group)
        if one_off:
            if income:
                t.one_off_income_cents += amount
            else:
                t.one_off_expense_cents -= amount
            if not one_offs:
                continue
        if income:
            t.income_cents += amount
        else:
            t.expense_cents -= amount
    if not totals:
        return []
    months = months_between(min(totals), max(totals))
    return [totals.get(m, MonthTotals(m)) for m in months]


def category_totals(
    session: Session,
    date_from: date | None = None,
    date_to: date | None = None,
    account_id: int | None = None,
    one_offs: bool = True,
) -> list[CategoryTotal]:
    """Expenses per category, largest first. Income categories are not included."""
    acc: dict[str, CategoryTotal] = {}
    rows = session.execute(_base_query(date_from, date_to, account_id))
    for _month, amount, cat_id, key, name, group, income_group, one_off, group_id in rows:
        if is_income(amount, cat_id, income_group) or (one_off and not one_offs):
            continue
        k = key or UNCATEGORIZED_KEY
        t = acc.setdefault(
            k, CategoryTotal(cat_id, k, group or "", name or "", 0, bool(one_off), group_id)
        )
        t.expense_cents -= amount
    return sorted(acc.values(), key=lambda t: t.expense_cents, reverse=True)


def category_trend(
    session: Session,
    category_id: int,
    date_from: date | None = None,
    date_to: date | None = None,
    account_id: int | None = None,
) -> list[tuple[str, int]]:
    """Signed monthly sums for one category (expenses negative, income positive); also
    for a one-off category (it was chosen explicitly)."""
    q = _base_query(date_from, date_to, account_id).where(Split.category_id == category_id)
    sums: dict[str, int] = defaultdict(int)
    for month, amount, *_ in session.execute(q):
        sums[month] += amount
    if not sums:
        return []
    return [(m, sums.get(m, 0)) for m in months_between(min(sums), max(sums))]


def shifted_categories(session: Session) -> list[tuple[str, int]]:
    """Categories with a reporting-month shift: (category name, days), in budget order."""
    rows = session.execute(
        select(Category.name, Category.report_shift_days)
        .join(CategoryGroup, Category.group_id == CategoryGroup.id)
        .where(Category.report_shift_days > 0, Category.hidden.is_(False))
        .order_by(CategoryGroup.sort, Category.sort)
    )
    return [(name, days) for name, days in rows]


def booking_date_range(session: Session) -> tuple[date | None, date | None]:
    first, last = session.execute(
        select(func.min(Transaction.booking_date), func.max(Transaction.booking_date))
    ).one()
    return first, last


def month_end(day: date) -> date:
    following = date(day.year + 1, 1, 1) if day.month == 12 else date(day.year, day.month + 1, 1)
    return following - timedelta(days=1)


def partial_month(session: Session, date_to: date) -> tuple[str, date] | None:
    """The last month of a period ending ``date_to`` if it holds data for only part of
    it: ("YYYY-MM", last day with data). The data ends at the period's end or at the
    latest booking (the last statement imported), whichever is earlier."""
    _, last = booking_date_range(session)
    if last is None:
        return None
    end = min(date_to, last)
    if end >= month_end(end):
        return None
    return f"{end:%Y-%m}", end


@dataclass
class YearRow:
    """Expenses of one category in two years."""

    key: str
    group_name: str
    category_name: str
    first: int = 0  # cents spent in the first year
    second: int = 0
    one_off: bool = False
    category_id: int | None = None  # None: uncategorized

    @property
    def difference(self) -> int:
        return self.second - self.first

    @property
    def change(self) -> float | None:
        """Relative change; None when nothing was spent in the first year."""
        return self.difference / self.first if self.first > 0 else None


@dataclass
class YearComparison:
    first_year: int
    second_year: int
    last_month: int  # both years from January to this month
    rows: list[YearRow]  # by expenses of the second year, largest first
    income: tuple[int, int]
    # one-off income and expenses of both years (included above only with one_offs=True)
    one_off_income: tuple[int, int] = (0, 0)
    one_off_expenses: tuple[int, int] = (0, 0)
    last_day: int | None = None  # and to this day of it (None: the month's end)

    @property
    def expenses(self) -> tuple[int, int]:
        return sum(r.first for r in self.rows), sum(r.second for r in self.rows)


def year_comparison(
    session: Session,
    first_year: int,
    second_year: int,
    last_month: int = 12,
    account_id: int | None = None,
    one_offs: bool = True,
    last_day: int | None = None,
) -> YearComparison:
    """Expenses per category (and income) of two years, each from January to
    ``last_month`` (to its day ``last_day``) - so that a year still in progress can be
    compared fairly: both years cut at the same day."""

    def period(year: int) -> tuple[date, date]:
        end = month_end(date(year, last_month, 1))
        if last_day is not None:
            end = end.replace(day=min(last_day, end.day))  # 29.02. in a common year: 28.
        return date(year, 1, 1), end

    rows: dict[str, YearRow] = {}
    income, one_off_income, one_off_expenses = [], [], []
    for attr, year in (("first", first_year), ("second", second_year)):
        start, end = period(year)
        for t in category_totals(session, start, end, account_id, one_offs):
            row = rows.setdefault(
                t.key,
                YearRow(
                    t.key,
                    t.group_name,
                    t.category_name,
                    one_off=t.one_off,
                    category_id=t.category_id,
                ),
            )
            setattr(row, attr, t.expense_cents)
        months = monthly_totals(session, start, end, account_id, one_offs)
        income.append(sum(m.income_cents for m in months))
        one_off_income.append(sum(m.one_off_income_cents for m in months))
        one_off_expenses.append(sum(m.one_off_expense_cents for m in months))
    ordered = sorted(rows.values(), key=lambda r: (r.second, r.first), reverse=True)
    return YearComparison(
        first_year,
        second_year,
        last_month,
        ordered,
        (income[0], income[1]),
        (one_off_income[0], one_off_income[1]),
        (one_off_expenses[0], one_off_expenses[1]),
        last_day,
    )


@dataclass
class PayeeTotal:
    key: str  # payee name with letters only (see recurring.payee_key)
    name: str  # the most frequent spelling
    category: str  # most frequent category ("" if none)
    expense_cents: int  # positive = spent; refunds reduce it
    count: int  # bookings

    @property
    def average_cents(self) -> int:
        return round(self.expense_cents / self.count) if self.count else 0


def top_payees(
    session: Session,
    date_from: date | None = None,
    date_to: date | None = None,
    account_id: int | None = None,
    one_offs: bool = True,
) -> list[PayeeTotal]:
    """Expenses per payee, largest first (as ``category_totals``: no transfers, no income)."""
    from hiro_budget.services.recurring import payee_key

    q = (
        select(
            Transaction.id,
            Transaction.merchant,
            Transaction.counterparty,
            Split.amount_cents,
            Split.category_id,
            Category.name,
            CategoryGroup.is_income,
            Category.one_off,
        )
        .join(Transaction, Split.transaction_id == Transaction.id)
        .outerjoin(Category, Split.category_id == Category.id)
        .outerjoin(CategoryGroup, Category.group_id == CategoryGroup.id)
        .where(Split.transfer_account_id.is_(None))
    )
    q = _period(q, date_from, date_to, account_id)
    totals: dict[str, int] = defaultdict(int)
    bookings: dict[str, set[int]] = defaultdict(set)
    names: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    categories: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    rows = session.execute(q)
    for tx_id, merchant, counterparty, amount, cat_id, cat_name, income_group, one_off in rows:
        if is_income(amount, cat_id, income_group) or (one_off and not one_offs):
            continue
        name = merchant or counterparty or ""
        key = payee_key(name)
        if not key:
            continue
        totals[key] -= amount
        bookings[key].add(tx_id)
        names[key][name] += 1
        if cat_name:
            categories[key][cat_name] -= amount
    result = [
        PayeeTotal(
            key,
            max(names[key], key=names[key].__getitem__),
            max(categories[key], key=categories[key].__getitem__) if categories[key] else "",
            cents,
            len(bookings[key]),
        )
        for key, cents in totals.items()
        if cents > 0
    ]
    return sorted(result, key=lambda p: p.expense_cents, reverse=True)
