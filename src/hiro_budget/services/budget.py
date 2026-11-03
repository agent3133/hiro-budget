"""Envelope budget in the style of "You Need A Budget".

Definitions (amounts in cents, months as "YYYY-MM"):

- Budget accounts ``B``: accounts with ``on_budget``. Budget categories: categories of
  groups that are not income groups.
- Activity(c, m): splits with category ``c`` on budget accounts booked in month ``m``
  (including categorized outflows to tracking accounts, e.g. mortgage principal).
- Inflow(m): what flows into "Ready to Assign": income categories, uncategorized splits
  and uncategorized transfers to/from tracking accounts on budget accounts. Transfers
  between two budget accounts count on both sides and cancel out; a side without its
  counterpart (e.g. booked before the other account's history starts) moves money into
  or out of the budget, as it does with the account balances.
- Start funds: balances of the budget accounts before the start month. An account whose
  opening balance lies after the start month brings it in as inflow of that month.
- Available(c, m) = carry + Assigned(c, m) + Activity(c, m), carry = max(Available(c, m-1), 0).
  Overspending (negative Available) is not carried; it is taken from the next month's
  Ready to Assign.
- Ready to Assign(m) = start funds + inflows up to m + overspending before m
  - everything assigned (all months, including future ones).

Money is assigned to *envelopes*: normally each category, but a group with
``budget: group`` is one envelope for all its categories (Activity is their sum; the
categories only show their activity).

Invariant: balance of the budget accounts at the end of ``m`` equals
Ready to Assign(m) + sum of Available(e, m) when nothing is assigned to later months.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from datetime import date
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from hiro_budget.config import Settings, TargetDef
from hiro_budget.db.models import (
    Account,
    BudgetAssignment,
    Category,
    CategoryGroup,
    Split,
    Transaction,
)
from hiro_budget.services.accounts import Anchor, anchors

# --- months ------------------------------------------------------------------------


def month_of(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def month_add(month: str, n: int) -> str:
    y, m = map(int, month.split("-"))
    total = y * 12 + (m - 1) + n
    return f"{total // 12:04d}-{total % 12 + 1:02d}"


def months_between(a: str, b: str) -> int:
    """Number of months from ``a`` to ``b`` (b - a)."""
    ya, ma = map(int, a.split("-"))
    yb, mb = map(int, b.split("-"))
    return (yb - ya) * 12 + (mb - ma)


def month_first_day(month: str) -> date:
    y, m = map(int, month.split("-"))
    return date(y, m, 1)


def month_last_day(month: str) -> date:
    nxt = month_first_day(month_add(month, 1))
    return date.fromordinal(nxt.toordinal() - 1)


# --- results -----------------------------------------------------------------------


@dataclass(frozen=True)
class Envelope:
    """What money is assigned to: a category, or a group budgeted as a whole."""

    kind: Literal["category", "group"]
    id: int

    @classmethod
    def of(cls, target: int | Envelope) -> Envelope:
        """Plain ints are category ids."""
        return target if isinstance(target, Envelope) else cls("category", target)


@dataclass
class CategoryMonth:
    """One budget line: a category, or (``is_group``) a group budgeted as a whole.

    Categories of a group budgeted as a whole have ``in_group_budget`` set; they show
    their activity only, assigned and available belong to the group.
    """

    category_id: int | None  # None for a group envelope
    key: str  # full category key, or the group key for a group envelope
    name: str
    group_key: str
    group_name: str
    hidden: bool
    group_id: int = 0
    is_group: bool = False
    in_group_budget: bool = False
    assigned: int = 0
    activity: int = 0
    available: int = 0
    carry: int = 0  # available brought over from the previous month (never negative)
    target: TargetDef | None = None
    target_month: int = 0  # what the target asks to assign this month
    needed: int = 0  # still to assign this month to satisfy the target
    progress: float | None = None  # 0..1 for targets
    due: str | None = None  # due month of date-based targets

    @property
    def envelope(self) -> Envelope:
        if self.is_group:
            return Envelope("group", self.group_id)
        assert self.category_id is not None  # only a group envelope has none
        return Envelope("category", self.category_id)

    @property
    def overspent(self) -> bool:
        return self.available < 0

    @property
    def underfunded(self) -> bool:
        return self.needed > 0


@dataclass
class GroupMonth:
    key: str
    name: str
    group_id: int = 0
    categories: list[CategoryMonth] = field(default_factory=list)
    budget: CategoryMonth | None = None  # the envelope of a group budgeted as a whole

    @property
    def budget_by_group(self) -> bool:
        return self.budget is not None

    @property
    def envelopes(self) -> list[CategoryMonth]:
        return [self.budget] if self.budget is not None else self.categories

    @property
    def carry(self) -> int:
        return sum(e.carry for e in self.envelopes)

    @property
    def assigned(self) -> int:
        return sum(e.assigned for e in self.envelopes)

    @property
    def activity(self) -> int:
        return sum(c.activity for c in self.categories)

    @property
    def available(self) -> int:
        return sum(e.available for e in self.envelopes)


@dataclass
class BudgetMonth:
    month: str
    start_month: str
    ready_to_assign: int
    inflow: int  # inflow into Ready to Assign in this month
    overspent_last_month: int  # (negative) overspending of the previous month taken now
    assigned_in_future: int  # assigned in months after this one
    # Ready to Assign = balance_last_month + inflow - carried_in - assigned_this_month
    #                   - assigned_in_future
    # (spending comes out of the categories, so it does not appear here)
    balance_last_month: int  # balance of the budget accounts at the end of last month
    carried_in: int  # what the envelopes kept from last month
    assigned_this_month: int  # everything assigned in this month
    balance: int  # balance of the budget accounts at the end of the month
    uncategorized_count: int  # bookings of this month that affect Ready to Assign
    uncategorized_amount: int
    groups: list[GroupMonth] = field(default_factory=list)

    @property
    def categories(self) -> list[CategoryMonth]:
        return [c for g in self.groups for c in g.categories]

    @property
    def envelopes(self) -> list[CategoryMonth]:
        """Budget lines money is assigned to (categories and groups budgeted as a whole)."""
        return [e for g in self.groups for e in g.envelopes]

    def category(self, category_id: int) -> CategoryMonth:
        return next(c for c in self.categories if c.category_id == category_id)

    def envelope(self, target: int | Envelope) -> CategoryMonth | None:
        env = Envelope.of(target)
        return next((e for e in self.envelopes if e.envelope == env), None)

    def group(self, group_id: int) -> GroupMonth | None:
        return next((g for g in self.groups if g.group_id == group_id), None)

    @property
    def carry(self) -> int:
        return sum(g.carry for g in self.groups)

    @property
    def assigned(self) -> int:
        return sum(g.assigned for g in self.groups)

    @property
    def activity(self) -> int:
        return sum(g.activity for g in self.groups)

    @property
    def available(self) -> int:
        return sum(g.available for g in self.groups)


# --- data loading ------------------------------------------------------------------


@dataclass
class _Flows:
    start_funds: int = 0
    inflow: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    activity: dict[tuple[int, str], int] = field(default_factory=lambda: defaultdict(int))
    uncategorized: dict[str, list[int]] = field(default_factory=lambda: defaultdict(list))
    # For reports (budget_report): activity by reporting month (``report_shift_days``);
    # the budget itself uses ``activity``.
    report_activity: dict[tuple[int, str], int] = field(default_factory=lambda: defaultdict(int))


def budget_account_ids(session: Session) -> set[int]:
    return set(session.scalars(select(Account.id).where(Account.on_budget.is_(True))))


def start_month(session: Session, settings: Settings) -> str:
    if settings.app.budget.start_month:
        return settings.app.budget.start_month
    first_booking = session.scalar(
        select(func.min(Transaction.booking_date))
        .join(Account, Transaction.account_id == Account.id)
        .where(Account.on_budget.is_(True))
    )
    first_opening = session.scalar(
        select(func.min(Account.opening_date)).where(Account.on_budget.is_(True))
    )
    candidates = [d for d in (first_booking, first_opening) if d is not None]
    return month_of(min(candidates) if candidates else date.today())


def _load_flows(
    session: Session, start: str, budget: set[int], account_anchors: dict[int, Anchor]
) -> _Flows:
    flows = _Flows()
    start_date = month_first_day(start)
    for acc_id in budget:
        a = account_anchors[acc_id]
        if a.date is not None and a.date >= start_date:
            flows.inflow[month_of(a.date)] += a.base_cents  # opening balance after start
        else:
            flows.start_funds += a.base_cents

    # Only the columns needed, not Split/Transaction objects: every month of the budget
    # reads all splits, and making ORM objects of them took most of the time.
    splits = session.execute(
        select(
            Split.amount_cents,
            Split.category_id,
            Split.transfer_account_id,
            CategoryGroup.is_income,
            Transaction.id,
            Transaction.account_id,
            Transaction.booking_date,
            Category.report_shift_days,
        )
        .join(Transaction, Split.transaction_id == Transaction.id)
        .outerjoin(Category, Split.category_id == Category.id)
        .outerjoin(CategoryGroup, Category.group_id == CategoryGroup.id)
        .where(Transaction.account_id.in_(budget))
    )
    for (
        amount,
        category_id,
        transfer_id,
        is_income,
        tx_id,
        account_id,
        booking_date,
        shift,
    ) in splits:
        a = account_anchors[account_id]
        if a.date is not None and booking_date < a.date:
            continue  # before the account's starting point (not part of its balance)
        if booking_date < start_date:
            flows.start_funds += amount
            continue
        m = month_of(booking_date)
        if category_id is not None and not is_income:
            flows.activity[(category_id, m)] += amount
            report_month = month_add(m, -1) if shift and booking_date.day <= shift else m
            flows.report_activity[(category_id, report_month)] += amount
        else:
            flows.inflow[m] += amount
            between_budget_accounts = transfer_id in budget
            if (
                category_id is None
                and not between_budget_accounts
                and (transfer_id is None or amount < 0)
            ):
                flows.uncategorized[m].append(tx_id)
    return flows


@dataclass
class _Lines:
    """Budget lines of all non-income groups and where assigned money goes."""

    groups: list[GroupMonth]
    envelopes: list[CategoryMonth]
    of_category: dict[int, Envelope]  # category id -> envelope it books into
    of_group: dict[int, Envelope]  # group id -> envelope for amounts stored on the group


def _budget_lines(session: Session, settings: Settings) -> _Lines:
    group_targets = {g.key: g.target for g in settings.categories.groups}
    targets = {
        f"{g.key}.{c.key}": c.target for g in settings.categories.groups for c in g.categories
    }
    groups = session.scalars(
        select(CategoryGroup)
        .where(CategoryGroup.is_income.is_(False))
        .order_by(CategoryGroup.sort, CategoryGroup.id)
        .options(selectinload(CategoryGroup.categories))
    ).all()
    lines = _Lines([], [], {}, {})
    for g in groups:
        gm = GroupMonth(g.key, g.name, g.id)
        # A group without categories can only hold money as a whole.
        by_group = bool(g.budget_by_group) or not g.categories
        if by_group:
            gm.budget = CategoryMonth(
                category_id=None,
                key=g.key,
                name=g.name,
                group_key=g.key,
                group_name=g.name,
                hidden=g.hidden,
                group_id=g.id,
                is_group=True,
                target=group_targets.get(g.key) if g.budget_by_group else None,
            )
            lines.envelopes.append(gm.budget)
            lines.of_group[g.id] = gm.budget.envelope
        for c in sorted(g.categories, key=lambda c: (c.sort, c.id)):
            cm = CategoryMonth(
                category_id=c.id,
                key=c.key,
                name=c.name,
                group_key=g.key,
                group_name=g.name,
                hidden=c.hidden or g.hidden,
                group_id=g.id,
                in_group_budget=by_group,
                target=None if by_group else targets.get(c.key),
            )
            gm.categories.append(cm)
            if gm.budget is not None:  # the group is budgeted as a whole
                lines.of_category[c.id] = gm.budget.envelope
            else:
                lines.envelopes.append(cm)
                lines.of_category[c.id] = cm.envelope
        if not by_group:
            # Amounts still stored on the group go to the category that takes the
            # group's money when switching back (see sync.first_category).
            visible = [c for c in gm.categories if not c.hidden] or gm.categories
            lines.of_group[g.id] = visible[0].envelope
        lines.groups.append(gm)
    return lines


def _assignments(
    rows: list[tuple[int | None, int | None, str, int]], lines: _Lines
) -> dict[tuple[Envelope, str], int]:
    result: dict[tuple[Envelope, str], int] = defaultdict(int)
    for category_id, group_id, month, cents in rows:
        if category_id is not None:
            env = lines.of_category.get(category_id, Envelope("category", category_id))
        else:
            assert group_id is not None  # an assignment has a category or a group
            env = lines.of_group.get(group_id, Envelope("group", group_id))
        result[(env, month)] += cents
    return result


@dataclass
class BudgetData:
    """What every month of the budget is computed from: loaded once, so that a report
    over many months (``budget_report``) does not read all bookings again for each."""

    start: str
    budget: set[int]  # budget account ids
    anchors: dict[int, Anchor]
    flows: _Flows
    assignments: list[tuple[int | None, int | None, str, int]]  # category, group, month, ¢

    @classmethod
    def load(cls, session: Session, settings: Settings) -> BudgetData:
        start = start_month(session, settings)
        budget = budget_account_ids(session)
        account_anchors = anchors(session)
        rows = session.execute(
            select(
                BudgetAssignment.category_id,
                BudgetAssignment.group_id,
                BudgetAssignment.month,
                BudgetAssignment.amount_cents,
            ).where(BudgetAssignment.month >= start)
        )
        return cls(
            start,
            budget,
            account_anchors,
            _load_flows(session, start, budget, account_anchors),
            [(c, g, m, a) for c, g, m, a in rows],
        )


# --- targets -----------------------------------------------------------------------


def _due_month(target: TargetDef, month: str) -> str | None:
    if target.date is None:
        return None
    due = month_of(target.date)
    if target.type == "repeating":
        while months_between(due, month) > 0:  # advance to the first due month >= month
            due = month_add(due, target.every_months)
    return due


def apply_target(c: CategoryMonth, month: str) -> None:
    t = c.target
    if t is None:
        return
    amount = round(t.amount * 100)
    if t.type == "monthly":
        c.target_month = amount
        c.needed = max(0, amount - c.assigned)
        c.progress = min(1.0, max(0, c.assigned) / amount)
        return
    due = _due_month(t, month)
    c.due = due
    funded = c.carry + c.assigned
    c.progress = min(1.0, max(0, funded) / amount)
    if due is None:  # savings balance without deadline: progress only
        return
    months_left = max(1, months_between(month, due) + 1)
    remaining = max(0, amount - c.carry)
    c.target_month = math.ceil(remaining / months_left)
    c.needed = max(0, c.target_month - c.assigned)


# --- computation -------------------------------------------------------------------


def _copy_lines(lines: _Lines) -> _Lines:
    """Fresh line objects for one month (``compute_months`` fills and filters them)."""
    groups = []
    for g in lines.groups:
        budget = replace(g.budget) if g.budget is not None else None
        groups.append(replace(g, categories=[replace(c) for c in g.categories], budget=budget))
    envelopes = [e for g in groups for e in g.envelopes]
    return _Lines(groups, envelopes, lines.of_category, lines.of_group)


def compute_month(
    session: Session,
    settings: Settings,
    month: str,
    data: BudgetData | None = None,
    balances: bool = True,
) -> BudgetMonth:
    """The budget of ``month``. ``data``: loaded before for several months;
    ``balances=False`` leaves out the account balances and the uncategorized amount
    (0), which take extra queries (reports that only need the envelopes)."""
    data = data or BudgetData.load(session, settings)
    if months_between(data.start, month) < 0:
        month = data.start
    return next(compute_months(session, settings, month, month, data, balances))


def compute_months(
    session: Session,
    settings: Settings,
    first: str,
    last: str,
    data: BudgetData | None = None,
    balances: bool = True,
) -> Iterator[BudgetMonth]:
    """The budgets of the months ``first`` to ``last`` (from the budget's start on) in one
    pass: each month carries what is available into the next. Computing each month on
    its own replayed every month before it, quadratic in the months (#59)."""
    data = data or BudgetData.load(session, settings)
    start = data.start
    flows = data.flows
    lines = _budget_lines(session, settings)
    assigned = _assignments(data.assignments, lines)
    keys = [e.envelope for e in lines.envelopes]

    activity: dict[tuple[Envelope, str], int] = defaultdict(int)
    for (cat_id, m), cents in flows.activity.items():
        activity[(lines.of_category[cat_id], m)] += cents
    assigned_total = sum(assigned.values())
    assigned_by_month: dict[str, int] = defaultdict(int)
    for (_e, m), v in assigned.items():
        assigned_by_month[m] += v

    available = dict.fromkeys(keys, 0)
    overspent_before = 0  # overspending of months before the current one
    overspent_prev = 0
    inflow_total = 0
    current = start
    while current <= last:
        overspent_this = 0
        numbers = []  # (carry, assigned, activity, available) per envelope
        for env in keys:
            carry = max(available[env], 0)
            a = assigned.get((env, current), 0)
            act = activity.get((env, current), 0)
            available[env] = carry + a + act
            overspent_this += min(available[env], 0)
            numbers.append((carry, a, act, available[env]))
        inflow_total += flows.inflow.get(current, 0)
        if current >= first:
            yield _month(
                session,
                data,
                _copy_lines(lines),
                current,
                numbers,
                ready_to_assign=flows.start_funds
                + inflow_total
                + overspent_before
                - assigned_total,
                overspent_prev=overspent_prev,
                assigned_future=sum(v for m, v in assigned_by_month.items() if m > current),
                assigned_now=assigned_by_month.get(current, 0),
                balances=balances,
            )
        overspent_before += overspent_this
        overspent_prev = overspent_this
        current = month_add(current, 1)


def _month(
    session: Session,
    data: BudgetData,
    lines: _Lines,
    month: str,
    numbers: list[tuple[int, int, int, int]],
    *,
    ready_to_assign: int,
    overspent_prev: int,
    assigned_future: int,
    assigned_now: int,
    balances: bool,
) -> BudgetMonth:
    flows = data.flows
    envelopes = lines.envelopes
    for e, (carry, a, act, avail) in zip(envelopes, numbers, strict=True):
        e.carry, e.assigned, e.activity, e.available = carry, a, act, avail
    carried_in = sum(e.carry for e in envelopes)

    for e in envelopes:
        apply_target(e, month)
    groups: list[GroupMonth] = []
    for g in lines.groups:
        if g.budget is not None:  # categories show their own activity only
            for c in g.categories:
                assert c.category_id is not None  # category lines of the group
                c.activity = flows.activity.get((c.category_id, month), 0)
        g.categories = [
            c for c in g.categories if not c.hidden or c.assigned or c.activity or c.available
        ]
        b = g.budget
        if b is not None and (b.assigned or b.activity or b.available):
            groups.append(g)
        elif g.categories and not (b is not None and b.hidden):
            groups.append(g)

    unc = flows.uncategorized.get(month, [])
    return BudgetMonth(
        month=month,
        start_month=data.start,
        ready_to_assign=ready_to_assign,
        inflow=flows.inflow.get(month, 0),
        overspent_last_month=overspent_prev,
        assigned_in_future=assigned_future,
        balance_last_month=(budget_balance(session, month_add(month, -1), data) if balances else 0),
        carried_in=carried_in,
        assigned_this_month=assigned_now,
        balance=budget_balance(session, month, data) if balances else 0,
        uncategorized_count=len(set(unc)),
        uncategorized_amount=_uncategorized_amount(session, unc) if balances else 0,
        groups=groups,
    )


def _uncategorized_amount(session: Session, tx_ids: list[int]) -> int:
    if not tx_ids:
        return 0
    return session.execute(
        select(func.coalesce(func.sum(Split.amount_cents), 0)).where(
            Split.transaction_id.in_(set(tx_ids)), Split.category_id.is_(None)
        )
    ).scalar_one()


def budget_balance(session: Session, month: str, data: BudgetData | None = None) -> int:
    """Balance of all budget accounts at the end of ``month`` (for checks and display)."""
    end = month_last_day(month)
    total = 0
    budget = data.budget if data else budget_account_ids(session)
    for acc_id, a in (data.anchors if data else anchors(session)).items():
        if acc_id not in budget or (a.date is not None and a.date > end):
            continue
        q = select(func.coalesce(func.sum(Transaction.amount_cents), 0)).where(
            Transaction.account_id == acc_id, Transaction.booking_date <= end
        )
        if a.date is not None:
            q = q.where(Transaction.booking_date >= a.date)
        total += a.base_cents + session.execute(q).scalar_one()
    return total


# --- operations --------------------------------------------------------------------


def _assignment_row(session: Session, month: str, env: Envelope) -> BudgetAssignment | None:
    column = BudgetAssignment.category_id if env.kind == "category" else BudgetAssignment.group_id
    return session.scalar(
        select(BudgetAssignment).where(BudgetAssignment.month == month, column == env.id)
    )


def get_assigned(session: Session, month: str, target: int | Envelope) -> int:
    """Assigned amount of an envelope (a plain int is a category id) in ``month``."""
    row = _assignment_row(session, month, Envelope.of(target))
    return row.amount_cents if row else 0


def set_assigned(session: Session, month: str, target: int | Envelope, cents: int) -> None:
    env = Envelope.of(target)
    row = _assignment_row(session, month, env)
    if cents == 0:
        if row is not None:
            session.delete(row)
    elif row is None:
        ids = {"category_id": env.id} if env.kind == "category" else {"group_id": env.id}
        session.add(BudgetAssignment(month=month, amount_cents=cents, **ids))
    else:
        row.amount_cents = cents
    session.flush()


def move_money(
    session: Session,
    month: str,
    from_id: int | Envelope | None,
    to_id: int | Envelope | None,
    cents: int,
) -> None:
    """Move assigned money between envelopes; ``None`` means Ready to Assign."""
    if cents <= 0:
        raise ValueError("the amount to move must be positive")
    source = Envelope.of(from_id) if from_id is not None else None
    target = Envelope.of(to_id) if to_id is not None else None
    if source == target:
        raise ValueError("source and target must differ")
    if source is not None:
        set_assigned(session, month, source, get_assigned(session, month, source) - cents)
    if target is not None:
        set_assigned(session, month, target, get_assigned(session, month, target) + cents)


def assignments_of(session: Session, month: str) -> dict[Envelope, int]:
    """Everything assigned in ``month`` (for undo/redo)."""
    return {
        _envelope_of(a): a.amount_cents
        for a in session.scalars(select(BudgetAssignment).where(BudgetAssignment.month == month))
    }


def _envelope_of(a: BudgetAssignment) -> Envelope:
    """An assignment's envelope: its category, else its group (one of them is set)."""
    if a.category_id is not None:
        return Envelope("category", a.category_id)
    assert a.group_id is not None
    return Envelope("group", a.group_id)


def restore_assignments(session: Session, month: str, amounts: dict[Envelope, int]) -> None:
    """Make the assignments of ``month`` exactly ``amounts``."""
    for env in set(assignments_of(session, month)) | set(amounts):
        set_assigned(session, month, env, amounts.get(env, 0))


def clear_month(session: Session, month: str) -> int:
    """Remove everything assigned in ``month``; returns the number of envelopes."""
    current = assignments_of(session, month)
    restore_assignments(session, month, {})
    return len(current)


def copy_last_month(session: Session, settings: Settings, month: str) -> int:
    """Assign last month's amounts to envelopes with nothing assigned yet; returns count."""
    prev = month_add(month, -1)
    previous = session.scalars(select(BudgetAssignment).where(BudgetAssignment.month == prev))
    count = 0
    for a in previous:
        env = _envelope_of(a)
        if get_assigned(session, month, env) == 0 and a.amount_cents:
            set_assigned(session, month, env, a.amount_cents)
            count += 1
    return count


@dataclass
class AssignResult:
    categories: int
    amount: int
    still_needed: int  # what could not be covered (Ready to Assign used up)


def assign_to_targets(session: Session, settings: Settings, month: str) -> AssignResult:
    """Fund underfunded targets in budget order as far as Ready to Assign allows."""
    b = compute_month(session, settings, month)
    rta = b.ready_to_assign
    count = total = missing = 0
    for e in b.envelopes:
        if e.needed <= 0:
            continue
        amount = min(e.needed, max(rta, 0))
        missing += e.needed - amount
        if amount <= 0:
            continue
        set_assigned(session, month, e.envelope, e.assigned + amount)
        rta -= amount
        count += 1
        total += amount
    return AssignResult(count, total, missing)


def cover_overspending(session: Session, settings: Settings, month: str) -> AssignResult:
    """Bring overspent envelopes back to zero from Ready to Assign, in budget order."""
    b = compute_month(session, settings, month)
    rta = b.ready_to_assign
    count = total = missing = 0
    for e in b.envelopes:
        if not e.overspent:
            continue
        amount = min(-e.available, max(rta, 0))
        missing += -e.available - amount
        if amount <= 0:
            continue
        set_assigned(session, month, e.envelope, e.assigned + amount)
        rta -= amount
        count += 1
        total += amount
    return AssignResult(count, total, missing)
