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

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

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
    # only the splits that pay on a loan or its interest, as plain columns (#60: all splits
    # of all other accounts as ORM objects took 0.5 s for ten years)
    q = (
        select(
            Transaction.booking_date,
            Split.transfer_account_id,
            Split.category_id,
            Split.memo,
            Split.amount_cents,
        )
        .join(Transaction, Split.transaction_id == Transaction.id)
        .where(
            Transaction.booking_date >= start,
            Transaction.booking_date <= end,
            Transaction.account_id.not_in(loan_ids),
            or_(
                Split.transfer_account_id.in_(loan_ids),
                and_(Split.category_id.in_(interest_ids), Split.transfer_account_id.is_(None)),
            ),
        )
        .order_by(Split.id)
    )
    for booking_date, target, category_id, memo, amount in session.execute(q):
        month = f"{booking_date:%Y-%m}"
        if target in loan_ids:
            p = payments.setdefault(month, MonthPayments(month))
            if memo == "Tilgung":  # principal of an instalment (mortgage rule)
                p.principal -= amount
            else:
                p.extra -= amount
        elif category_id in interest_ids and target is None:
            payments.setdefault(month, MonthPayments(month)).interest -= amount

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


# --- forecast (#47) ---------------------------------------------------------------------

FORECAST_MAX_MONTHS = 720  # 60 years: an instalment that never pays the loan off stops here
INSTALMENT_MONTHS = 6  # the usual instalment: the most frequent one of these last months
EXTRA_MONTH = 12  # a yearly special repayment is made in December


@dataclass
class Forecast:
    """The loan paid on with the latest instalment (interest rate and amount stay), and
    optionally a special repayment every December."""

    start: str  # month of the instalment the forecast starts from
    payment: int  # monthly instalment, interest + principal (cents)
    rate: float  # nominal interest rate per year (0.021 = 2.1 %)
    extra_per_year: int  # special repayment each December (cents)
    months: list[tuple[str, int]] = field(default_factory=list)  # month, debt at its end
    interest: int = 0  # interest still to pay until paid off
    fixed_until: str | None = None  # end of the fixed-rate period ("YYYY-MM")
    debt_at_fixed_end: int | None = None

    @property
    def payoff(self) -> str | None:
        """The month of the last instalment, None when not paid off within the limit."""
        return self.months[-1][0] if self.months and self.months[-1][1] == 0 else None


def _next_month(month: str) -> str:
    y, m = int(month[:4]), int(month[5:])
    return f"{y + m // 12}-{m % 12 + 1:02d}"


def forecast(
    data: Mortgage, extra_per_year: int = 0, fixed_until: date | None = None
) -> Forecast | None:
    """From the latest regular instalment: the usual amount of the last months (a month
    with two instalments or none, booked at a turn of the month, does not count), its
    interest over the debt before it as the rate. None without such an instalment, or
    when the instalment does not even cover the interest."""
    regular = [
        (i, m)
        for i, m in enumerate(data.months)
        if m.interest > 0 and m.principal > 0 and m.debt is not None
    ]
    recent = [m.interest + m.principal for _, m in regular[-INSTALMENT_MONTHS:]]
    if not recent:
        return None
    payment = max(set(recent), key=lambda v: (recent.count(v), -recent[::-1].index(v)))
    i, last = next((i, m) for i, m in reversed(regular) if m.interest + m.principal == payment)
    assert last.debt is not None  # regular months have a debt
    before = data.months[i - 1].debt if i else None
    if before is None:
        before = last.debt + last.principal + last.extra
    if before <= 0:
        return None
    monthly = last.interest / before
    result = Forecast(
        last.month,
        payment,
        monthly * 12,
        extra_per_year,
        fixed_until=f"{fixed_until:%Y-%m}" if fixed_until else None,
    )
    debt, month = last.debt, last.month
    if result.fixed_until is not None and result.fixed_until <= month:
        result.debt_at_fixed_end = None  # already over
    while debt > 0 and len(result.months) < FORECAST_MAX_MONTHS:
        month = _next_month(month)
        interest = round(debt * monthly)
        principal = min(payment - interest, debt)
        if principal <= 0:
            return None  # the instalment does not cover the interest
        debt -= principal
        result.interest += interest
        if extra_per_year and int(month[5:]) == EXTRA_MONTH and debt > 0:
            debt -= min(extra_per_year, debt)
        result.months.append((month, debt))
        if month == result.fixed_until:
            result.debt_at_fixed_end = debt
    return result
