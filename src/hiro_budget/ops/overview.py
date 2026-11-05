"""Operation: the overview page as text."""

from __future__ import annotations

from datetime import date

from pydantic import Field

from hiro_budget.ops import Args, OpContext, Operation, Result
from hiro_budget.ops.text import dot, eur, gap_text, money


class OverviewArgs(Args):
    pass


class NameAmount(Result):
    name: str
    amount: str = Field(description="euros, e.g. '-12.34'")


class OverviewResult(Result):
    month: str
    ready_to_assign: str
    overspent: list[NameAmount]
    underfunded_categories: int
    uncategorized_bookings: int
    bookings_to_review: int
    income_this_month: str
    expenses_this_month: str
    net_worth: str | None
    gaps: list[str]
    stale_accounts: list[str]
    overdue: list[str] = Field(
        description="expected payments whose due date has passed without a booking"
    )
    upcoming: list[str] = Field(description="expected payments in the next 30 days")


def _today() -> date:
    return date.today()


def run(ctx: OpContext, a: OverviewArgs) -> OverviewResult:
    from hiro_budget.services.overview import overview

    today = _today()
    with ctx.session() as s:
        o = overview(s, ctx.settings, today)

    def line(r) -> str:
        return f"{r.next_due:%Y-%m-%d} {r.payee} {money(r.amount_cents)}"

    return OverviewResult(
        month=o.month,
        ready_to_assign=dot(o.ready_to_assign),
        overspent=[NameAmount(name=n, amount=dot(c)) for n, c in o.overspent],
        underfunded_categories=o.underfunded,
        uncategorized_bookings=o.uncategorized,
        bookings_to_review=o.to_review,
        income_this_month=dot(o.income),
        expenses_this_month=dot(o.expenses),
        net_worth=dot(o.net_worth_now) if o.net_worth_now is not None else None,
        gaps=[gap_text(acc, g) for acc, g in o.gaps],
        stale_accounts=[
            f"{x.name}: nothing since {x.last:%Y-%m-%d} ({x.days} days)" for x in o.stale
        ],
        overdue=[line(r) for r in o.upcoming if r.next_due < today],
        upcoming=[line(r) for r in o.upcoming if r.next_due >= today],
    )


def render(r: OverviewResult, width: int | None = None) -> str:
    lines = [
        f"Overview {r.month}",
        f"Ready to Assign:      {eur(r.ready_to_assign)}",
        f"Income this month:    {eur(r.income_this_month)}",
        f"Expenses this month:  {eur(r.expenses_this_month)}",
        f"Net worth:            {eur(r.net_worth)}",
        f"Uncategorized:        {r.uncategorized_bookings} bookings",
        f"To review:            {r.bookings_to_review} suggestions",
        f"Underfunded:          {r.underfunded_categories} categories",
    ]
    if r.overspent:
        lines.append("Overspent:")
        lines += [f"  {x.name}  {eur(x.amount)}" for x in r.overspent]
    if r.overdue:
        lines.append("Overdue:")
        lines += [f"  {x}" for x in r.overdue]
    if r.upcoming:
        lines.append("Upcoming:")
        lines += [f"  {x}" for x in r.upcoming]
    if r.gaps:
        lines.append("Statement gaps:")
        lines += [f"  {x}" for x in r.gaps]
    if r.stale_accounts:
        lines.append("Stale accounts:")
        lines += [f"  {x}" for x in r.stale_accounts]
    return "\n".join(lines)


OPERATIONS = [
    Operation(
        name="get_overview",
        cli=("overview",),
        summary="Show the overview: Ready to Assign, open work, this month, net worth",
        description=(
            "The overview page as data: Ready to Assign, overspent categories, number of "
            "uncategorized bookings and suggestions to review, income and expenses of the "
            "current month, net worth, statement gaps, stale accounts, overdue payments "
            "(expected, not booked) and upcoming payments. "
            "Amounts are euros as strings with a dot."
        ),
        args=OverviewArgs,
        result=OverviewResult,
        run=run,
        render=render,
        read_only=True,
    )
]
