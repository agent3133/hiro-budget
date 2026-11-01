"""Mortgage progress: interest, repayments and remaining debt of the loan accounts.

Payments are taken from the paying side (e.g. the Girokonto), not from the counter-
bookings on the loan account:

- Interest: splits in a mortgage's interest category that are no transfer.
- Repayment: the principal part of a mortgage instalment (split by the mortgage rule,
  memo "Tilgung" - also after it was edited by hand).
- Special repayment (Sondertilgung): any other transfer to a loan account.

The remaining debt per month end comes from ``networth``.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from hiro_budget.config import Settings
from hiro_budget.db.models import Account, AccountKind, Category, Split, Transaction
from hiro_budget.services import networth
from hiro_budget.services.reports import months_between


@dataclass
class MonthPayments:
    month: str
    interest: int = 0  # cents paid
    principal: int = 0
    extra: int = 0
    debt: int | None = None  # remaining debt at the month end (positive), if known

    @property
    def total(self) -> int:
        return self.interest + self.principal + self.extra


@dataclass
class Mortgage:
    loans: list[str]  # names of the loan accounts
    months: list[MonthPayments] = field(default_factory=list)

    def years(self) -> list[tuple[int, MonthPayments]]:
        """Sums per calendar year; ``debt`` is the remaining debt at the year's last month."""
        result: dict[int, MonthPayments] = {}
        for m in self.months:
            year = int(m.month[:4])
            y = result.setdefault(year, MonthPayments(str(year)))
            y.interest += m.interest
            y.principal += m.principal
            y.extra += m.extra
            if m.debt is not None:
                y.debt = m.debt
        return sorted(result.items())


def mortgage_progress(
    session: Session, settings: Settings, start: date, end: date, account_id: int | None = None
) -> Mortgage:
    """Payments and remaining debt from ``start`` to ``end``; one loan account when
    ``account_id`` is one, else all loan accounts."""
    loans = list(session.scalars(select(Account).where(Account.kind == AccountKind.LOAN)))
    if account_id is not None and any(a.id == account_id for a in loans):
        loans = [a for a in loans if a.id == account_id]
    result = Mortgage([a.name for a in loans])
    if not loans:
        return result
    loan_ids = {a.id for a in loans}
    interest_keys = {m.interest_category for m in settings.app.mortgage}
    interest_ids = set(session.scalars(select(Category.id).where(Category.key.in_(interest_keys))))

    payments: dict[str, MonthPayments] = {}
    q = (
        select(Split)
        .join(Transaction, Split.transaction_id == Transaction.id)
        .options(selectinload(Split.transaction))
        .where(
            Transaction.booking_date >= start,
            Transaction.booking_date <= end,
            Transaction.account_id.not_in(loan_ids),
        )
    )
    for s in session.scalars(q):
        month = f"{s.transaction.booking_date:%Y-%m}"
        if s.transfer_account_id in loan_ids:
            p = payments.setdefault(month, MonthPayments(month))
            if s.memo == "Tilgung":  # principal of an instalment (mortgage rule)
                p.principal -= s.amount_cents
            else:
                p.extra -= s.amount_cents
        elif s.category_id in interest_ids and s.transfer_account_id is None:
            payments.setdefault(month, MonthPayments(month)).interest -= s.amount_cents

    debts: dict[str, int] = defaultdict(int)
    known: set[str] = set()
    for loan in loans:
        history = networth.net_worth(session, f"{end:%Y-%m}", loan.id)
        for m, cents in history.accounts[0].balances.items() if history.accounts else []:
            if cents is not None:
                debts[m] -= cents  # debt as a positive number
                known.add(m)
    months = sorted(set(payments) | {m for m in known if m >= f"{start:%Y-%m}"})
    if not months:
        return result
    for m in months_between(months[0], months[-1]):
        p = payments.get(m, MonthPayments(m))
        p.debt = debts[m] if m in known else None
        result.months.append(p)
    return result
