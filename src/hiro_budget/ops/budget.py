"""Operation: the budget of a month (groups, categories, Ready to Assign)."""

from __future__ import annotations

from pydantic import Field

from hiro_budget.ops import Args, OpContext, Operation, Result, positional, resolve
from hiro_budget.ops.text import Column, dot, eur, money, table


class BudgetShowArgs(Args):
    month: str | None = Field(
        None,
        description="month as YYYY-MM (default: the current month)",
        json_schema_extra=positional(nargs="?"),
    )


class BudgetLine(Result):
    key: str = Field(description="category key 'group.category', or the group key")
    name: str
    assigned: str
    activity: str
    available: str
    target: str | None = Field(description="the target in words, if the line has one")
    needed: str | None = Field(description="still to assign this month to meet the target")
    hidden: bool = False
    budgeted_by_group: bool = Field(False, description="activity only: the group holds the money")


class BudgetGroup(Result):
    key: str
    name: str
    budget_by_group: bool
    assigned: str
    activity: str
    available: str
    lines: list[BudgetLine]


class BudgetShowResult(Result):
    month: str
    ready_to_assign: str
    inflow: str
    assigned_this_month: str
    overspent_last_month: str
    uncategorized_bookings: int
    uncategorized_amount: str
    groups: list[BudgetGroup]


def target_text(target) -> str | None:
    if target is None:
        return None
    amount = f"{target.amount:.2f}"
    match target.type:
        case "monthly":
            return f"{amount} every month"
        case "by_date":
            return f"{amount} by {target.date}"
        case "repeating":
            return f"{amount} every {target.every_months} months from {target.date}"
        case _:
            return f"build up to {amount}" + (f" by {target.date}" if target.date else "")


def _line(c, hidden_ids: set[int | None], by_group: bool = False) -> BudgetLine:
    return BudgetLine(
        key=c.key,
        name=c.name,
        assigned=dot(c.assigned),
        activity=dot(c.activity),
        available=dot(c.available),
        target=target_text(c.target),
        needed=dot(c.needed) if c.target is not None else None,
        hidden=c.category_id in hidden_ids,
        budgeted_by_group=by_group,
    )


def run(ctx: OpContext, a: BudgetShowArgs) -> BudgetShowResult:
    from sqlalchemy import select

    from hiro_budget.db.models import Category
    from hiro_budget.services import budget

    month = resolve.month(a.month)
    with ctx.session() as s:
        b = budget.compute_month(s, ctx.settings, month)
        hidden = set(s.scalars(select(Category.id).where(Category.hidden.is_(True))))
    groups = []
    for g in b.groups:
        if g.budget is not None:
            lines = [_line(g.budget, hidden)] + [_line(c, hidden, True) for c in g.categories]
        else:
            lines = [_line(c, hidden) for c in g.categories]
        groups.append(
            BudgetGroup(
                key=g.key,
                name=g.name,
                budget_by_group=g.budget_by_group,
                assigned=dot(g.assigned),
                activity=dot(g.activity),
                available=dot(g.available),
                lines=[ln for ln in lines if not ln.hidden or ln.assigned != "0.00"],
            )
        )
    return BudgetShowResult(
        month=b.month,
        ready_to_assign=dot(b.ready_to_assign),
        inflow=dot(b.inflow),
        assigned_this_month=dot(b.assigned_this_month),
        overspent_last_month=dot(b.overspent_last_month),
        uncategorized_bookings=b.uncategorized_count,
        uncategorized_amount=dot(b.uncategorized_amount),
        groups=groups,
    )


def render(r: BudgetShowResult, width: int | None = None) -> str:
    head = [
        f"Budget {r.month}",
        f"Ready to Assign: {eur(r.ready_to_assign)}",
        f"Inflow {eur(r.inflow)}, assigned {eur(r.assigned_this_month)}",
    ]
    if r.uncategorized_bookings:
        head.append(
            f"{r.uncategorized_bookings} uncategorized bookings this month "
            f"({eur(r.uncategorized_amount)}) affect Ready to Assign"
        )
    cols = [
        Column("Category", shrink=True),
        Column("Assigned", "r"),
        Column("Activity", "r"),
        Column("Available", "r"),
        Column("Target", shrink=True),
    ]
    rows = []
    for g in r.groups:
        rows.append(
            [
                g.name,
                eur(g.assigned),
                eur(g.activity),
                eur(g.available),
                "(group)" if g.budget_by_group else "",
            ]
        )
        for line in g.lines:
            name = f"  {line.name}"
            if line.budgeted_by_group:
                rows.append([name, "", eur(line.activity), "", ""])
            else:
                rows.append(
                    [
                        name,
                        eur(line.assigned),
                        eur(line.activity),
                        eur(line.available),
                        _target(line),
                    ]
                )
    return "\n".join([*head, "", table(cols, rows, width)])


def _target(line: BudgetLine) -> str:
    if line.target is None:
        return ""
    need = (
        f" (need {money(round(float(line.needed or 0) * 100))})"
        if line.needed not in (None, "0.00")
        else ""
    )
    return f"{line.target}{need}"


OPERATIONS = [
    Operation(
        name="get_budget",
        cli=("budget", "show"),
        summary="Show the budget of a month",
        description=(
            "The budget of one month (YYYY-MM, default the current month): Ready to Assign, "
            "then per group and category what was assigned, the activity (spending) and what "
            "is available, plus targets. Groups budgeted as a whole hold the money on the "
            "group line; their categories show activity only. Amounts are euros as strings."
        ),
        args=BudgetShowArgs,
        result=BudgetShowResult,
        run=run,
        render=render,
        read_only=True,
    )
]
