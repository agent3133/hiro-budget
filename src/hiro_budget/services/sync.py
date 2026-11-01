"""Synchronize YAML configuration (categories, own accounts) into the database.

YAML is the source of truth. Rows are matched by stable keys/names; entries removed
from YAML are hidden, never deleted, so historic splits keep their category.

When a group switches between budgeting per category and as a whole (``budget: group``),
its budget assignments are converted so that Ready to Assign stays unchanged: category
amounts are summed into the group, or the group amount goes to its first category.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from hiro_budget.config import CategoriesConfig, ConfigError, OwnAccount
from hiro_budget.db.models import (
    Account,
    AccountKind,
    BudgetAssignment,
    Category,
    CategoryGroup,
)


def check_key_renames(session: Session, groups: dict[str, str], categories: dict[str, str]) -> None:
    """Refuse renames onto keys that other (e.g. removed, hidden) rows still use."""
    for model, renames in ((CategoryGroup, groups), (Category, categories)):
        taken = set(session.scalars(select(model.key))) - set(renames)
        clash = sorted(new for new in renames.values() if new in taken)
        if clash:
            raise ConfigError(
                f"key '{clash[0]}' is still used by a removed category or group; choose another key"
            )


def rename_keys(session: Session, groups: dict[str, str], categories: dict[str, str]) -> None:
    """Rename groups and categories in place, so bookings and budgets stay with them."""
    check_key_renames(session, groups, categories)
    for model, renames in ((CategoryGroup, groups), (Category, categories)):
        rows = [
            (row, renames[row.key])
            for row in session.scalars(select(model).where(model.key.in_(list(renames))))
        ]
        # Two steps, so that keys can be swapped without hitting the unique constraint.
        for row, _ in rows:
            row.key = f"~renaming~{row.id}"
        session.flush()
        for row, new in rows:
            row.key = new
        session.flush()


def sync_categories(session: Session, cfg: CategoriesConfig) -> None:
    groups = {g.key: g for g in session.scalars(select(CategoryGroup))}
    cats = {c.key: c for c in session.scalars(select(Category))}
    seen_groups: set[str] = set()
    seen_cats: set[str] = set()
    switched: list[CategoryGroup] = []

    for gi, gdef in enumerate(cfg.groups):
        group = groups.get(gdef.key)
        if group is None:
            group = CategoryGroup(key=gdef.key, budget_by_group=gdef.budget_by_group)
            session.add(group)
            groups[gdef.key] = group
        elif bool(group.budget_by_group) != gdef.budget_by_group:
            switched.append(group)
        group.name, group.sort, group.is_income, group.hidden = gdef.name, gi, gdef.income, False
        group.budget_by_group = gdef.budget_by_group
        seen_groups.add(gdef.key)

        for ci, cdef in enumerate(gdef.categories):
            key = f"{gdef.key}.{cdef.key}"
            cat = cats.get(key)
            if cat is None:
                cat = Category(key=key)
                session.add(cat)
                cats[key] = cat
            cat.group, cat.name, cat.sort, cat.hidden = group, cdef.name, ci, False
            cat.one_off, cat.report_shift_days = cdef.one_off, cdef.report_shift_days
            seen_cats.add(key)

    for key, group in groups.items():
        if key not in seen_groups:
            group.hidden = True
    for key, cat in cats.items():
        if key not in seen_cats:
            cat.hidden = True
    session.flush()
    for group in switched:
        convert_group_assignments(session, group)


def convert_group_assignments(session: Session, group: CategoryGroup) -> None:
    """Move the group's assignments to match ``group.budget_by_group``."""
    cat_ids = [c.id for c in group.categories]
    if group.budget_by_group:
        rows = session.scalars(
            select(BudgetAssignment).where(BudgetAssignment.category_id.in_(cat_ids))
        ).all()
        sums: dict[str, int] = {}
        for row in rows:
            sums[row.month] = sums.get(row.month, 0) + row.amount_cents
            session.delete(row)
        session.flush()
        for month, cents in sums.items():
            _add(session, month, cents, group_id=group.id)
    else:
        target = first_category(group)
        if target is None:
            return  # no category to take the money; the group keeps it
        rows = session.scalars(
            select(BudgetAssignment).where(BudgetAssignment.group_id == group.id)
        ).all()
        for row in rows:
            month, cents = row.month, row.amount_cents
            session.delete(row)
            session.flush()
            _add(session, month, cents, category_id=target.id)
    session.flush()


def first_category(group: CategoryGroup) -> Category | None:
    """Category that takes a group's money when it is budgeted per category again."""
    visible = [c for c in group.categories if not c.hidden]
    return (visible or list(group.categories) or [None])[0]


def _add(
    session: Session,
    month: str,
    cents: int,
    *,
    category_id: int | None = None,
    group_id: int | None = None,
) -> None:
    row = session.scalar(
        select(BudgetAssignment).where(
            BudgetAssignment.month == month,
            BudgetAssignment.category_id.is_(None)
            if category_id is None
            else BudgetAssignment.category_id == category_id,
            BudgetAssignment.group_id.is_(None)
            if group_id is None
            else BudgetAssignment.group_id == group_id,
        )
    )
    if row is None:
        row = BudgetAssignment(
            month=month, category_id=category_id, group_id=group_id, amount_cents=0
        )
        session.add(row)
    row.amount_cents += cents
    if row.amount_cents == 0:
        session.delete(row)


def sync_accounts(session: Session, own_accounts: list[OwnAccount]) -> None:
    existing = {a.name: a for a in session.scalars(select(Account))}
    by_iban = {a.iban: a for a in existing.values() if a.iban}
    for acc in own_accounts:
        row = existing.get(acc.name) or (by_iban.get(acc.iban) if acc.iban else None)
        if row is None:
            row = Account(name=acc.name)
            session.add(row)
        row.name = acc.name
        row.kind = AccountKind(acc.kind)
        row.institution = acc.institution
        row.is_own = True
        if acc.iban:
            row.iban = acc.iban
        row.opening_balance_cents = (
            round(acc.opening_balance * 100) if acc.opening_balance is not None else None
        )
        row.opening_date = acc.opening_date
        row.on_budget = acc.is_on_budget
    session.flush()
