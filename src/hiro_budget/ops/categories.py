"""Operation: categories and groups."""

from __future__ import annotations

from pydantic import Field

from hiro_budget.ops import Args, OpContext, Operation, Result
from hiro_budget.ops.budget import target_text
from hiro_budget.ops.text import Column, table


class ListCategoriesArgs(Args):
    pass


class CategoryInfo(Result):
    key: str = Field(description="full key 'group.category': use it wherever a category is asked")
    name: str
    hidden: bool
    one_off: bool = Field(description="irregular: reports can leave it out")
    report_shift_days: int
    target: str | None


class GroupInfo(Result):
    key: str
    name: str
    income: bool
    budget: str = Field(description="'category' or 'group' (budgeted as a whole)")
    target: str | None
    categories: list[CategoryInfo]


class ListCategoriesResult(Result):
    groups: list[GroupInfo]


def run(ctx: OpContext, a: ListCategoriesArgs) -> ListCategoriesResult:
    from sqlalchemy import select

    from hiro_budget.db.models import Category

    with ctx.session() as s:
        hidden = {k for (k,) in s.execute(select(Category.key).where(Category.hidden.is_(True)))}
    groups = []
    for g in ctx.settings.categories.groups:
        cats = [
            CategoryInfo(
                key=f"{g.key}.{c.key}",
                name=c.name,
                hidden=f"{g.key}.{c.key}" in hidden,
                one_off=c.one_off,
                report_shift_days=c.report_shift_days,
                target=target_text(c.target),
            )
            for c in g.categories
        ]
        groups.append(
            GroupInfo(
                key=g.key,
                name=g.name,
                income=g.income,
                budget=g.budget,
                target=target_text(g.target),
                categories=cats,
            )
        )
    return ListCategoriesResult(groups=groups)


def render(r: ListCategoriesResult, width: int | None = None) -> str:
    cols = [
        Column("Key"),
        Column("Name", shrink=True),
        Column("Flags"),
        Column("Target", shrink=True),
    ]
    rows = []
    for g in r.groups:
        flags = [
            f for f, on in (("income", g.income), ("budget by group", g.budget == "group")) if on
        ]
        rows.append([g.key, g.name, ", ".join(flags), g.target or ""])
        for c in g.categories:
            flags = [
                f
                for f, on in (
                    ("hidden", c.hidden),
                    ("one-off", c.one_off),
                    (f"shift {c.report_shift_days}d", c.report_shift_days > 0),
                )
                if on
            ]
            rows.append([f"  {c.key}", c.name, ", ".join(flags), c.target or ""])
    return table(cols, rows, width)


OPERATIONS = [
    Operation(
        name="list_categories",
        cli=("categories", "list"),
        summary="List category groups and categories with their keys",
        description=(
            "All category groups and their categories, in budget order. Categories are "
            "addressed by their key ('group.category') or exact name in the other tools. "
            "Check this list before creating a category. Shows hidden, one-off and targets."
        ),
        args=ListCategoriesArgs,
        result=ListCategoriesResult,
        run=run,
        render=render,
        read_only=True,
    )
]
