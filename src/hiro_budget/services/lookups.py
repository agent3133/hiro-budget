"""Small read-only lookups the views fill their pickers and colours with (#78: moved out of
the GUI layer, which only calls services)."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from hiro_budget.db.models import Account, Category, CategoryGroup, Transaction


def accounts(session: Session, *, by_name: bool = True) -> list[Account]:
    """All accounts, by name (else in the database's order)."""
    query = select(Account)
    if by_name:
        query = query.order_by(Account.name)
    return list(session.scalars(query))


def account_name(session: Session, account_id: int) -> str | None:
    account = session.get(Account, account_id)
    return account.name if account is not None else None


def own_accounts(session: Session) -> list[Account]:
    """The user's own accounts by name (transfer targets)."""
    return list(session.scalars(select(Account).where(Account.is_own).order_by(Account.name)))


def payee_names(session: Session) -> list[str]:
    """Every counterparty that occurs, sorted (for completion)."""
    return sorted(p for p in session.scalars(select(Transaction.counterparty).distinct()) if p)


def visible_groups(session: Session) -> list[CategoryGroup]:
    """The category groups that are not hidden, in budget order."""
    return list(
        session.scalars(
            select(CategoryGroup)
            .where(CategoryGroup.hidden.is_(False))
            .order_by(CategoryGroup.sort)
        )
    )


def expense_group_ids(session: Session) -> list[int]:
    """Ids of the expense groups in budget order (their chip colours follow it)."""
    return list(
        session.scalars(
            select(CategoryGroup.id)
            .where(CategoryGroup.is_income.is_(False))
            .order_by(CategoryGroup.sort, CategoryGroup.id)
        )
    )


def categories_by_id(session: Session) -> dict[int, Category]:
    return {c.id: c for c in session.scalars(select(Category))}
