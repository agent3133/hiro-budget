"""Operations: the reports (monthly, categories, years, payees, budget, net worth, explain)."""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import Field

from hiro_budget.ops import Args, OpContext, Operation, OpError, Result, flag, positional, resolve
from hiro_budget.ops.text import Column, day, dot, eur, table


class PeriodArgs(Args):
    """The period options of the reports page (whole history when left out)."""

    date_from: date | None = Field(
        None, description="first day (YYYY-MM-DD)", json_schema_extra=flag("--from")
    )
    date_to: date | None = Field(
        None, description="last day (YYYY-MM-DD)", json_schema_extra=flag("--to")
    )
    account: str | None = Field(None, description="only this account (name or IBAN)")
    one_offs: bool = Field(
        True,
        description="include the categories marked one-off (irregular income and expenses)",
        json_schema_extra=flag("--no-one-offs"),
    )


def _account_id(ctx: OpContext, s, name: str | None) -> int | None:
    return resolve.account(s, name).id if name else None


# --- monthly -------------------------------------------------------------------------


class MonthRow(Result):
    month: str
    income: str
    expenses: str
    net: str = Field(description="income minus expenses")
    one_off_income: str = Field(description="one-off income (counted only if one_offs)")
    one_off_expenses: str


class MonthlyResult(Result):
    months: list[MonthRow]
    income: str
    expenses: str
    net: str


def run_monthly(ctx: OpContext, a: PeriodArgs) -> MonthlyResult:
    from hiro_budget.services import reports

    with ctx.session() as s:
        months = reports.monthly_totals(
            s, a.date_from, a.date_to, _account_id(ctx, s, a.account), a.one_offs
        )
    return MonthlyResult(
        months=[
            MonthRow(
                month=m.month,
                income=dot(m.income_cents),
                expenses=dot(m.expense_cents),
                net=dot(m.net_cents),
                one_off_income=dot(m.one_off_income_cents),
                one_off_expenses=dot(m.one_off_expense_cents),
            )
            for m in months
        ],
        income=dot(sum(m.income_cents for m in months)),
        expenses=dot(sum(m.expense_cents for m in months)),
        net=dot(sum(m.net_cents for m in months)),
    )


def render_monthly(r: MonthlyResult, width: int | None = None) -> str:
    cols = [Column("Month"), Column("Income", "r"), Column("Expenses", "r"), Column("Net", "r")]
    rows = [[m.month, eur(m.income), eur(m.expenses), eur(m.net)] for m in r.months]
    rows.append(["Total", eur(r.income), eur(r.expenses), eur(r.net)])
    return table(cols, rows, width)


# --- categories ----------------------------------------------------------------------


class CategoryRow(Result):
    key: str
    group: str
    name: str
    expenses: str = Field(description="money spent (positive); refunds reduce it")
    one_off: bool


class CategoriesReportResult(Result):
    categories: list[CategoryRow]
    total: str


def run_categories(ctx: OpContext, a: PeriodArgs) -> CategoriesReportResult:
    from hiro_budget.services import reports

    with ctx.session() as s:
        totals = reports.category_totals(
            s, a.date_from, a.date_to, _account_id(ctx, s, a.account), a.one_offs
        )
    return CategoriesReportResult(
        categories=[
            CategoryRow(
                key=t.key,
                group=t.group_name,
                name=t.category_name or "(uncategorized)",
                expenses=dot(t.expense_cents),
                one_off=t.one_off,
            )
            for t in totals
        ],
        total=dot(sum(t.expense_cents for t in totals)),
    )


def render_categories(r: CategoriesReportResult, width: int | None = None) -> str:
    cols = [Column("Group", shrink=True), Column("Category", shrink=True), Column("Expenses", "r")]
    rows = [[c.group, c.name, eur(c.expenses)] for c in r.categories]
    rows.append(["Total", "", eur(r.total)])
    return table(cols, rows, width)


# --- years ---------------------------------------------------------------------------


class YearsArgs(Args):
    first_year: int = Field(
        description="the earlier year", ge=1900, le=2200, json_schema_extra=positional()
    )
    second_year: int = Field(
        description="the later year", ge=1900, le=2200, json_schema_extra=positional()
    )
    last_month: int = Field(12, ge=1, le=12, description="both years from January to this month")
    last_day: int | None = Field(
        None, ge=1, le=31, description="and to this day of that month (default: its end)"
    )
    account: str | None = Field(None, description="only this account (name or IBAN)")
    one_offs: bool = Field(
        True, description="include one-off categories", json_schema_extra=flag("--no-one-offs")
    )


class YearRowInfo(Result):
    key: str
    group: str
    name: str
    first: str
    second: str
    difference: str
    change_percent: float | None = Field(description="None when nothing was spent before")


class YearsResult(Result):
    first_year: int
    second_year: int
    last_month: int
    rows: list[YearRowInfo]
    income: list[str] = Field(description="[first year, second year]")
    expenses: list[str] = Field(description="[first year, second year]")


def run_years(ctx: OpContext, a: YearsArgs) -> YearsResult:
    from hiro_budget.services import reports

    with ctx.session() as s:
        y = reports.year_comparison(
            s,
            a.first_year,
            a.second_year,
            a.last_month,
            _account_id(ctx, s, a.account),
            a.one_offs,
            a.last_day,
        )
    return YearsResult(
        first_year=y.first_year,
        second_year=y.second_year,
        last_month=y.last_month,
        rows=[
            YearRowInfo(
                key=r.key,
                group=r.group_name,
                name=r.category_name or "(uncategorized)",
                first=dot(r.first),
                second=dot(r.second),
                difference=dot(r.difference),
                change_percent=None if r.change is None else round(r.change * 100, 1),
            )
            for r in y.rows
        ],
        income=[dot(v) for v in y.income],
        expenses=[dot(v) for v in y.expenses],
    )


def render_years(r: YearsResult, width: int | None = None) -> str:
    cols = [
        Column("Group", shrink=True),
        Column("Category", shrink=True),
        Column(str(r.first_year), "r"),
        Column(str(r.second_year), "r"),
        Column("Change", "r"),
    ]
    rows = [
        [
            x.group,
            x.name,
            eur(x.first),
            eur(x.second),
            "" if x.change_percent is None else f"{x.change_percent:+.0f} %",
        ]
        for x in r.rows
    ]
    rows.append(["Expenses", "", eur(r.expenses[0]), eur(r.expenses[1]), ""])
    rows.append(["Income", "", eur(r.income[0]), eur(r.income[1]), ""])
    return table(cols, rows, width)


# --- payees --------------------------------------------------------------------------


class PayeesArgs(PeriodArgs):
    limit: int = Field(25, ge=1, le=500, description="how many payees (largest first)")


class PayeeRow(Result):
    name: str
    category: str
    expenses: str
    bookings: int
    average: str


class PayeesResult(Result):
    payees: list[PayeeRow]
    total_payees: int


def run_payees(ctx: OpContext, a: PayeesArgs) -> PayeesResult:
    from hiro_budget.services import reports

    with ctx.session() as s:
        payees = reports.top_payees(
            s, a.date_from, a.date_to, _account_id(ctx, s, a.account), a.one_offs
        )
    return PayeesResult(
        payees=[
            PayeeRow(
                name=p.name,
                category=p.category,
                expenses=dot(p.expense_cents),
                bookings=p.count,
                average=dot(p.average_cents),
            )
            for p in payees[: a.limit]
        ],
        total_payees=len(payees),
    )


def render_payees(r: PayeesResult, width: int | None = None) -> str:
    cols = [
        Column("Payee", shrink=True),
        Column("Category", shrink=True),
        Column("Expenses", "r"),
        Column("Bookings", "r"),
        Column("Average", "r"),
    ]
    rows = [
        [p.name, p.category, eur(p.expenses), str(p.bookings), eur(p.average)] for p in r.payees
    ]
    note = f"\n{len(r.payees)} of {r.total_payees} payees" if r.total_payees > len(r.payees) else ""
    return table(cols, rows, width) + note


# --- budget vs. actual ---------------------------------------------------------------


class BudgetReportArgs(Args):
    first_month: str = Field(description="first month, YYYY-MM", json_schema_extra=positional())
    last_month: str | None = Field(
        None,
        description="last month, YYYY-MM (default: the first)",
        json_schema_extra=positional(nargs="?"),
    )
    only_budgeted: bool = Field(False, description="skip months in which nothing was assigned")
    one_offs: bool = Field(
        True, description="include one-off categories", json_schema_extra=flag("--no-one-offs")
    )


class BudgetLineRow(Result):
    key: str
    group: str
    name: str
    assigned: str
    spent: str
    difference: str = Field(description="assigned minus spent; negative: overspent")
    is_group: bool


class BudgetReportResult(Result):
    first_month: str
    last_month: str
    assigned: str
    spent: str
    difference: str
    lines: list[BudgetLineRow]


def run_budget_report(ctx: OpContext, a: BudgetReportArgs) -> BudgetReportResult:
    from hiro_budget.services.budget_report import budget_vs_actual

    first = resolve.month(a.first_month)
    last = resolve.month(a.last_month or a.first_month)
    with ctx.session() as s:
        r = budget_vs_actual(s, ctx.settings, first, last, a.only_budgeted, a.one_offs)
    return BudgetReportResult(
        first_month=first,
        last_month=last,
        assigned=dot(r.assigned),
        spent=dot(r.spent),
        difference=dot(r.assigned - r.spent),
        lines=[
            BudgetLineRow(
                key=x.key,
                group=x.group_name,
                name=x.name,
                assigned=dot(x.assigned),
                spent=dot(x.spent),
                difference=dot(x.difference),
                is_group=x.is_group,
            )
            for x in r.lines
        ],
    )


def render_budget_report(r: BudgetReportResult, width: int | None = None) -> str:
    cols = [
        Column("Group", shrink=True),
        Column("Line", shrink=True),
        Column("Assigned", "r"),
        Column("Spent", "r"),
        Column("Difference", "r"),
    ]
    rows = [
        [
            x.group,
            x.name + (" (group)" if x.is_group else ""),
            eur(x.assigned),
            eur(x.spent),
            eur(x.difference),
        ]
        for x in r.lines
    ]
    rows.append(["Total", "", eur(r.assigned), eur(r.spent), eur(r.difference)])
    return f"Budget vs. actual {r.first_month} to {r.last_month}\n" + table(cols, rows, width)


# --- net worth -----------------------------------------------------------------------


class NetWorthArgs(Args):
    last_month: str | None = Field(None, description="last month, YYYY-MM (default: this month)")
    months: int = Field(12, ge=1, le=240, description="how many months to show (newest last)")
    account: str | None = Field(None, description="only this account (name or IBAN)")


class NetWorthMonth(Result):
    month: str
    assets: str
    debts: str
    net: str


class NetWorthResult(Result):
    months: list[NetWorthMonth]
    accounts: dict[str, str] = Field(description="balance per account in the last month")


def run_net_worth(ctx: OpContext, a: NetWorthArgs) -> NetWorthResult:
    from hiro_budget.services.networth import net_worth

    with ctx.session() as s:
        w = net_worth(s, resolve.month(a.last_month), _account_id(ctx, s, a.account))
    shown = w.months[-a.months :]
    return NetWorthResult(
        months=[
            NetWorthMonth(
                month=m, assets=dot(w.assets(m)), debts=dot(w.debts(m)), net=dot(w.net(m))
            )
            for m in shown
        ],
        accounts={acc.name: dot(acc.value(shown[-1])) for acc in w.accounts} if shown else {},
    )


def render_net_worth(r: NetWorthResult, width: int | None = None) -> str:
    cols = [Column("Month"), Column("Assets", "r"), Column("Debts", "r"), Column("Net worth", "r")]
    rows = [[m.month, eur(m.assets), eur(m.debts), eur(m.net)] for m in r.months]
    out = [table(cols, rows, width)]
    if r.accounts:
        out.append(f"Accounts in {r.months[-1].month}:")
        out += [f"  {name}  {eur(v)}" for name, v in r.accounts.items()]
    return "\n".join(out)


# --- explain -------------------------------------------------------------------------


class ExplainArgs(PeriodArgs):
    what: Literal["income", "expenses", "saved", "budget"] = Field(
        description=(
            "which number to explain: a report number (income, expenses, saved) over the "
            "period, or 'budget' (the spending counted by budget vs. actual, with --months)"
        ),
        json_schema_extra=positional(),
    )
    category: str | None = Field(None, description="only this category (key or name)")
    months: list[str] | None = Field(
        None, description="for 'budget': the months (YYYY-MM), e.g. 2026-10 2026-11"
    )


class ExplainBooking(Result):
    id: int
    date: str
    report_month: str
    payee: str
    purpose: str
    account: str
    amount: str


class ExplainPart(Result):
    section: str
    category: str
    group: str
    total: str
    bookings: list[ExplainBooking]


class ExplainResult(Result):
    what: str
    total: str = Field(description="the number itself")
    parts: list[ExplainPart]
    left_out: str = Field(description="one-off categories not counted, euros")


def run_explain(ctx: OpContext, a: ExplainArgs) -> ExplainResult:
    from hiro_budget.services import drilldown

    with ctx.session() as s:
        category_ids = {resolve.category(s, a.category).id} if a.category else None
        if a.what == "budget":
            if not a.months:
                raise OpError("'budget' needs --months, e.g. --months 2026-10 2026-11")
            months = [resolve.month(m) for m in a.months]
            if category_ids is None:
                from hiro_budget.services.budget_report import budget_vs_actual

                rep = budget_vs_actual(s, ctx.settings, min(months), max(months))
                category_ids = set(rep.category_ids)
            b = drilldown.budget_breakdown(s, ctx.settings, months, category_ids)
        else:
            b = drilldown.report_breakdown(
                s,
                a.what,
                a.date_from,
                a.date_to,
                _account_id(ctx, s, a.account),
                a.one_offs,
                category_ids,
            )
    parts = [
        ExplainPart(
            section=sec.kind,
            category=p.name or "(uncategorized)",
            group=p.group_name,
            total=dot(p.cents),
            bookings=[
                ExplainBooking(
                    id=x.tx_id,
                    date=x.booking_date.isoformat(),
                    report_month=x.report_month,
                    payee=x.payee,
                    purpose=x.purpose,
                    account=x.account,
                    amount=dot(x.cents),
                )
                for x in p.bookings
            ],
        )
        for sec in b.sections
        for p in sec.parts
    ]
    return ExplainResult(
        what=a.what, total=dot(b.total), parts=parts, left_out=dot(b.left_out_cents)
    )


def render_explain(r: ExplainResult, width: int | None = None) -> str:
    out = [f"{r.what}: {eur(r.total)}"]
    for p in r.parts:
        out.append(f"\n{p.section}: {p.group} / {p.category}  {eur(p.total)}")
        cols = [
            Column("Id", "r"),
            Column("Date"),
            Column("Payee", shrink=True),
            Column("Amount", "r"),
        ]
        rows = [
            [
                str(x.id),
                day(date.fromisoformat(x.date)) + ("*" if x.report_month != x.date[:7] else ""),
                x.payee,
                eur(x.amount),
            ]
            for x in p.bookings
        ]
        out.append(table(cols, rows, width))
    if any(b.report_month != b.date[:7] for p in r.parts for b in p.bookings):
        out.append("* counted in another month than the booking date (report shift)")
    if r.left_out != "0.00":
        out.append(f"Left out (one-off): {eur(r.left_out)}")
    return "\n".join(out)


OPERATIONS = [
    Operation(
        name="report_monthly",
        cli=("report", "monthly"),
        summary="Income, expenses and net per month",
        description=(
            "Income and expenses per month for a period (whole history if no dates), "
            "optionally for one account. Transfers between own accounts are not counted. "
            "Same numbers as the reports page. Amounts are euros as strings."
        ),
        args=PeriodArgs,
        result=MonthlyResult,
        run=run_monthly,
        render=render_monthly,
        read_only=True,
    ),
    Operation(
        name="report_categories",
        cli=("report", "categories"),
        summary="Expenses per category",
        description=(
            "Expenses per category for a period, largest first (income is not included). "
            "Set one_offs false to leave out categories marked one-off."
        ),
        args=PeriodArgs,
        result=CategoriesReportResult,
        run=run_categories,
        render=render_categories,
        read_only=True,
    ),
    Operation(
        name="report_years",
        cli=("report", "years"),
        summary="Compare the expenses of two years",
        description=(
            "Expenses per category in two years, each from January to last_month (and to "
            "last_day), so that a running year compares fairly with the one before."
        ),
        args=YearsArgs,
        result=YearsResult,
        run=run_years,
        render=render_years,
        read_only=True,
    ),
    Operation(
        name="report_payees",
        cli=("report", "payees"),
        summary="Expenses per payee",
        description="The payees with the highest expenses in a period, largest first.",
        args=PayeesArgs,
        result=PayeesResult,
        run=run_payees,
        render=render_payees,
        read_only=True,
    ),
    Operation(
        name="report_budget_vs_actual",
        cli=("report", "budget"),
        summary="Budget vs. actual: assigned and spent per category",
        description=(
            "For a range of months: what was assigned and what was spent per budget line "
            "(category, or a group budgeted as a whole), and the difference."
        ),
        args=BudgetReportArgs,
        result=BudgetReportResult,
        run=run_budget_report,
        render=render_budget_report,
        read_only=True,
    ),
    Operation(
        name="report_net_worth",
        cli=("report", "net-worth"),
        summary="Net worth by month",
        description="Assets, debts and net worth at the end of each month, and per account.",
        args=NetWorthArgs,
        result=NetWorthResult,
        run=run_net_worth,
        render=render_net_worth,
        read_only=True,
    ),
    Operation(
        name="explain_value",
        cli=("report", "explain"),
        summary="Show the bookings behind a report or budget number",
        description=(
            "The bookings a number is made of: 'income', 'expenses' or 'saved' over a "
            "period (as the reports page), or 'budget' for the spending that budget vs. "
            "actual counts in some months. Optionally limited to one category."
        ),
        args=ExplainArgs,
        result=ExplainResult,
        run=run_explain,
        render=render_explain,
        read_only=True,
    ),
]
