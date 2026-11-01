"""Depot development: the value per depot statement and what was invested.

Each imported Depotauszug gives the market value per security on its date. "Invested"
starts with the value of the first statement in the period and adds the purchases and
subtracts the sales booked on the depot account up to each later statement, so
``value - invested`` is the price gain since the first statement. Distributions are
paid to another account and are not part of it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from hiro_budget.db.models import Account, AccountKind, Transaction
from hiro_budget.services.accounts import valuations


@dataclass
class Statement:
    day: date
    value: int  # cents, market value of the statement
    invested: int  # first value of the period + purchases - sales up to this day
    securities: dict[str, int] = field(default_factory=dict)  # name -> value

    @property
    def gain(self) -> int:
        return self.value - self.invested


@dataclass
class DepotHistory:
    account: str
    statements: list[Statement]  # oldest first

    def names(self) -> list[str]:
        """Securities of all statements, in order of first appearance."""
        seen: dict[str, None] = {}
        for s in self.statements:
            for name in s.securities:
                seen.setdefault(name, None)
        return list(seen)


def depot_accounts(session: Session) -> list[Account]:
    return list(
        session.scalars(
            select(Account).where(Account.kind == AccountKind.DEPOT).order_by(Account.name)
        )
    )


def depot_history(session: Session, account_id: int, start: date, end: date) -> DepotHistory:
    account = session.get_one(Account, account_id)
    found = [v for v in reversed(valuations(session, account_id)) if start <= v.date <= end]
    statements: list[Statement] = []
    for v in found:
        if statements:
            prev = statements[-1]
            booked = session.scalar(
                select(func.coalesce(func.sum(Transaction.amount_cents), 0)).where(
                    Transaction.account_id == account_id,
                    Transaction.booking_date > prev.day,
                    Transaction.booking_date <= v.date,
                )
            )
            invested = prev.invested + booked
        else:
            invested = v.total_cents
        securities: dict[str, int] = {}
        for h in sorted(v.holdings, key=lambda h: h.sort):
            securities[h.name] = securities.get(h.name, 0) + h.value_cents
        statements.append(Statement(v.date, v.total_cents, invested, securities))
    return DepotHistory(account.name, statements)
