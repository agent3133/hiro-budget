"""How a report number adds up: sections (income, expenses) -> categories -> bookings.

``report_breakdown`` follows the definitions of ``reports`` (no transfers between own
accounts, reporting month, one-offs), ``budget_breakdown`` those of Budget vs. actual
(budget accounts, categorized spending including categorized transfers). Amounts are
signed as the report shows them: income positive, expenses positive (refunds negative).
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from hiro_budget.config import MAX_REPORT_SHIFT_DAYS, Settings
from hiro_budget.db.models import Account, Category, CategoryGroup, Split, Transaction
from hiro_budget.services import budget
from hiro_budget.services.accounts import anchors
from hiro_budget.services.reports import (
    UNCATEGORIZED_KEY,
    is_income,
    report_date,
    report_splits,
)

Measure = Literal["income", "expenses", "saved"]


@dataclass
class BookingPart:
    tx_id: int
    booking_date: date
    report_month: str  # "YYYY-MM": differs from the booking month when shifted
    payee: str
    purpose: str
    account: str
    cents: int  # as counted in the number (expenses positive)

    @property
    def shifted(self) -> bool:
        return self.report_month != f"{self.booking_date:%Y-%m}"


@dataclass
class CategoryPart:
    category_id: int | None  # None: uncategorized
    key: str
    name: str
    group_name: str
    one_off: bool = False
    bookings: list[BookingPart] = field(default_factory=list)

    @property
    def cents(self) -> int:
        return sum(b.cents for b in self.bookings)


@dataclass
class Section:
    kind: Literal["income", "expenses"]
    parts: list[CategoryPart] = field(default_factory=list)  # largest first

    @property
    def cents(self) -> int:
        return sum(p.cents for p in self.parts)


@dataclass
class Breakdown:
    sections: list[Section]  # income before expenses
    left_out: list[CategoryPart] = field(default_factory=list)  # one-offs not counted

    @property
    def total(self) -> int:
        """The number itself: income − expenses when both are there."""
        if len(self.sections) == 1:
            return self.sections[0].cents
        return sum(s.cents if s.kind == "income" else -s.cents for s in self.sections)

    @property
    def left_out_cents(self) -> int:
        return sum(p.cents for p in self.left_out)


def _payee(merchant: str | None, counterparty: str | None) -> str:
    return merchant or counterparty or ""


class _Collector:
    """Groups booking parts by section and category."""

    def __init__(self) -> None:
        self.parts: dict[tuple[str, str], CategoryPart] = {}

    def add(self, section: str, category: CategoryPart, booking: BookingPart) -> None:
        part = self.parts.setdefault((section, category.key), category)
        part.bookings.append(booking)

    def section(self, kind: Literal["income", "expenses"]) -> Section:
        parts = [p for (s, _k), p in self.parts.items() if s == kind]
        for p in parts:
            p.bookings.sort(key=lambda b: (b.booking_date, b.tx_id))
        parts.sort(key=lambda p: p.cents, reverse=True)
        return Section(kind, parts)


def report_breakdown(
    session: Session,
    measure: Measure,
    date_from: date | None,
    date_to: date | None,
    account_id: int | None = None,
    one_offs: bool = True,
    category_ids: Collection[int | None] | None = None,
    only_one_offs: bool = False,
) -> Breakdown:
    """The parts of income, expenses or saved (income − expenses) in a period, as in
    ``reports.monthly_totals`` / ``category_totals``. ``category_ids`` limits it to some
    categories (None in it: uncategorized); ``only_one_offs`` to one-off categories."""
    q = report_splits(
        Transaction.id,
        Transaction.booking_date,
        func.strftime("%Y-%m", report_date()),
        Transaction.merchant,
        Transaction.counterparty,
        Transaction.purpose,
        Account.name,
        Split.amount_cents,
        Split.category_id,
        Category.key,
        Category.name,
        CategoryGroup.name,
        CategoryGroup.is_income,
        Category.one_off,
        date_from=date_from,
        date_to=date_to,
        account_id=account_id,
    ).join(Account, Transaction.account_id == Account.id)
    counted, left_out = _Collector(), _Collector()
    for (
        tx_id,
        booking_date,
        month,
        merchant,
        counterparty,
        purpose,
        account,
        amount,
        cat_id,
        key,
        name,
        group,
        income_group,
        one_off,
    ) in session.execute(q):
        if category_ids is not None and cat_id not in category_ids:
            continue
        if only_one_offs and not one_off:
            continue
        income = is_income(amount, cat_id, income_group)
        kind = "income" if income else "expenses"
        if measure != "saved" and kind != measure:
            continue
        category = CategoryPart(
            cat_id, key or UNCATEGORIZED_KEY, name or "", group or "", bool(one_off)
        )
        booking = BookingPart(
            tx_id,
            booking_date,
            month,
            _payee(merchant, counterparty),
            purpose or "",
            account,
            amount if income else -amount,
        )
        target = left_out if one_off and not one_offs else counted
        target.add(kind, category, booking)
    kinds: list[Literal["income", "expenses"]] = (
        ["income", "expenses"] if measure == "saved" else [measure]
    )
    return Breakdown(
        [counted.section(k) for k in kinds],
        [p for k in kinds for p in left_out.section(k).parts],
    )


def budget_breakdown(
    session: Session, settings: Settings, months: Collection[str], category_ids: Collection[int]
) -> Breakdown:
    """What Budget vs. actual counts as spent for these categories in these months:
    categorized splits on budget accounts (also transfers with a category, e.g. the
    principal of a mortgage), from the budget's start and each account's starting point,
    by reporting month."""
    if not months or not category_ids:
        return Breakdown([Section("expenses")])
    start = budget.start_month(session, settings)
    budget_ids = budget.budget_account_ids(session)
    account_anchors = anchors(session)
    first = budget.month_first_day(min(months))
    last = budget.month_last_day(max(months)) + timedelta(days=MAX_REPORT_SHIFT_DAYS)
    q = (
        select(
            Transaction.id,
            Transaction.booking_date,
            Transaction.account_id,
            Transaction.merchant,
            Transaction.counterparty,
            Transaction.purpose,
            Account.name,
            Split.amount_cents,
            Split.category_id,
            Category.key,
            Category.name,
            CategoryGroup.name,
            Category.one_off,
            Category.report_shift_days,
        )
        .join(Transaction, Split.transaction_id == Transaction.id)
        .join(Account, Transaction.account_id == Account.id)
        .join(Category, Split.category_id == Category.id)
        .join(CategoryGroup, Category.group_id == CategoryGroup.id)
        .where(
            Split.category_id.in_(set(category_ids)),
            Transaction.account_id.in_(budget_ids),
            Transaction.booking_date >= first,
            Transaction.booking_date <= last,
        )
    )
    start_date = budget.month_first_day(start)
    wanted = set(months)
    collector = _Collector()
    for (
        tx_id,
        booking_date,
        acc_id,
        merchant,
        counterparty,
        purpose,
        account,
        amount,
        cat_id,
        key,
        name,
        group,
        one_off,
        shift,
    ) in session.execute(q):
        # the same conditions as budget._load_flows
        a = account_anchors[acc_id]
        if (a.date is not None and booking_date < a.date) or booking_date < start_date:
            continue
        month = budget.month_of(booking_date)
        if shift and booking_date.day <= shift:
            month = budget.month_add(month, -1)
        if month not in wanted:
            continue
        collector.add(
            "expenses",
            CategoryPart(cat_id, key, name, group, bool(one_off)),
            BookingPart(
                tx_id,
                booking_date,
                month,
                _payee(merchant, counterparty),
                purpose or "",
                account,
                -amount,
            ),
        )
    return Breakdown([collector.section("expenses")])
