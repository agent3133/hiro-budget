"""Savings rate per month: the share of the income that was not spent.

Income and expenses as in the overview report: transfers between own accounts are no
expenses, so investments and loan repayments count as saved; interest is an expense.
The 12-month rate is total saved / total income of the 12 months up to a month, counted
only from the first month for which all accounts have data (see ``networth``), so that a
history that starts later on some accounts does not distort it. Months are reporting
months, and one-off categories can be left out (see ``reports``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy.orm import Session

from hiro_budget.services import networth
from hiro_budget.services.reports import monthly_totals, partial_month

MIN_ROLLING_MONTHS = 3


@dataclass
class SavingsMonth:
    month: str
    income_cents: int
    expense_cents: int
    rolling_income: int = 0  # up to 12 complete months up to this one
    rolling_saved: int = 0
    rolling_months: int = 0
    # of one-off categories (in income/expenses above only with one_offs=True)
    one_off_income_cents: int = 0
    one_off_expense_cents: int = 0

    @property
    def one_off_net_cents(self) -> int:
        return self.one_off_income_cents - self.one_off_expense_cents

    @property
    def saved_cents(self) -> int:
        return self.income_cents - self.expense_cents

    @property
    def rate(self) -> float | None:
        """Share of the income that was not spent; None without income."""
        return self.saved_cents / self.income_cents if self.income_cents > 0 else None

    @property
    def rolling_rate(self) -> float | None:
        """Savings rate of up to 12 months (saved / income, not an average of rates)."""
        if self.rolling_months < MIN_ROLLING_MONTHS or self.rolling_income <= 0:
            return None
        return self.rolling_saved / self.rolling_income


@dataclass
class SavingsRates:
    months: list[SavingsMonth]
    complete_from: str | None  # first month with data on all accounts
    # the last month if it holds data for only part of it: (month, last day with data);
    # it has no 12-month rate (its missing weeks would pull it down)
    partial: tuple[str, date] | None = None

    @property
    def income_cents(self) -> int:
        return sum(m.income_cents for m in self.months)

    @property
    def saved_cents(self) -> int:
        return sum(m.saved_cents for m in self.months)

    @property
    def rate(self) -> float | None:
        income = self.income_cents
        return self.saved_cents / income if income > 0 else None

    @property
    def one_off_income_cents(self) -> int:
        return sum(m.one_off_income_cents for m in self.months)

    @property
    def one_off_expense_cents(self) -> int:
        return sum(m.one_off_expense_cents for m in self.months)


def savings_rates(
    session: Session,
    date_from: date,
    date_to: date,
    account_id: int | None = None,
    one_offs: bool = True,
) -> SavingsRates:
    y, m = date_from.year, date_from.month - 11  # 11 months before, for the 12-month rate
    while m < 1:
        y, m = y - 1, m + 12
    totals = monthly_totals(session, date(y, m, 1), date_to, account_id, one_offs)
    worth = networth.net_worth(session, f"{date_to:%Y-%m}", account_id)
    complete_from = worth.complete_from if worth.months else None
    first = f"{date_from:%Y-%m}"
    partial = partial_month(session, date_to)
    months: list[SavingsMonth] = []
    for i, t in enumerate(totals):
        if t.month < first:
            continue
        window = [
            w
            for w in totals[max(0, i - 11) : i + 1]
            if complete_from is not None and w.month >= complete_from
        ]
        if partial is not None and t.month >= partial[0]:
            window = []  # an incomplete month: no 12-month rate
        months.append(
            SavingsMonth(
                t.month,
                t.income_cents,
                t.expense_cents,
                sum(w.income_cents for w in window),
                sum(w.net_cents for w in window),
                len(window),
                t.one_off_income_cents,
                t.one_off_expense_cents,
            )
        )
    return SavingsRates(months, complete_from, partial)
