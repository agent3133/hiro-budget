"""Operations that change the budget: assign, move, fund targets, cover, copy."""

from __future__ import annotations

from pydantic import Field

from hiro_budget.ops import (
    Args,
    Changed,
    OpContext,
    Operation,
    OpError,
    positional,
    render_changed,
    resolve,
)
from hiro_budget.ops.text import dot

READY = "ready"  # the word for Ready to Assign in source/target


class MonthArg(Args):
    month: str | None = Field(
        None,
        description="month as YYYY-MM (default: the current month)",
        json_schema_extra=positional(nargs="?"),
    )


def _envelope(s, text: str):
    """An envelope by category (key or name) or ``group:<key>`` for a group budgeted as a
    whole. Categories of such a group have no money of their own."""
    from hiro_budget.services.budget import Envelope

    if text.startswith("group:"):
        g = resolve.group(s, text.removeprefix("group:"))
        if not g.budget_by_group:
            raise OpError(f"group '{g.key}' is budgeted per category; assign to its categories")
        return Envelope("group", g.id), g.name
    c = resolve.category(s, text)
    if c.group.budget_by_group:
        raise OpError(
            f"'{c.key}' belongs to group '{c.group.key}', which is budgeted as a whole; "
            f"use group:{c.group.key}"
        )
    return Envelope("category", c.id), c.name


def _ready(ctx: OpContext, s, month: str) -> str:
    from hiro_budget.services import budget

    return dot(budget.compute_month(s, ctx.settings, month).ready_to_assign)


# --- assign ---------------------------------------------------------------------------


class AssignArgs(Args):
    category: str = Field(
        description="category key or name, or group:<key> for a group budgeted as a whole",
        json_schema_extra=positional(),
    )
    amount: str = Field(
        description="euros to assign in the month (a total, or a change with relative)",
        json_schema_extra=positional(),
    )
    month: str | None = Field(None, description="month as YYYY-MM (default: the current month)")
    relative: bool = Field(
        False, description="add the amount to what is assigned (can be negative)"
    )


def run_assign(ctx: OpContext, a: AssignArgs) -> Changed:
    from hiro_budget.services import budget

    month = resolve.month(a.month)
    with ctx.write("assign_budget", a), ctx.session() as s:
        env, name = _envelope(s, a.category)
        old = budget.get_assigned(s, month, env)
        new = resolve.cents(a.amount) + (old if a.relative else 0)
        if new < 0:
            raise OpError("the assigned amount cannot be negative; move money out instead")
        budget.set_assigned(s, month, env, new)
        ready = _ready(ctx, s, month)
    return Changed(
        message=f"{name} {month}: assigned {dot(new)}",
        details=[f"was {dot(old)}"],
        ready_to_assign=ready,
    )


# --- move -----------------------------------------------------------------------------


class MoveArgs(Args):
    source: str = Field(
        description=f"category to take money from, or '{READY}' for Ready to Assign",
        json_schema_extra=positional(),
    )
    target: str = Field(
        description=f"category to give money to, or '{READY}' for Ready to Assign",
        json_schema_extra=positional(),
    )
    amount: str = Field(description="euros to move (positive)", json_schema_extra=positional())
    month: str | None = Field(None, description="month as YYYY-MM (default: the current month)")


def run_move(ctx: OpContext, a: MoveArgs) -> Changed:
    from hiro_budget.services import budget

    month = resolve.month(a.month)
    with ctx.write("move_money", a), ctx.session() as s:
        src = None if a.source.lower() == READY else _envelope(s, a.source)
        dst = None if a.target.lower() == READY else _envelope(s, a.target)
        cents = resolve.cents(a.amount)
        if src is not None and budget.get_assigned(s, month, src[0]) < cents:
            raise OpError(f"{src[1]} has less than {dot(cents)} assigned in {month}")
        budget.move_money(s, month, src[0] if src else None, dst[0] if dst else None, cents)
        ready = _ready(ctx, s, month)
    return Changed(
        message=f"moved {dot(cents)} from {src[1] if src else 'Ready to Assign'} "
        f"to {dst[1] if dst else 'Ready to Assign'} in {month}",
        ready_to_assign=ready,
    )


# --- targets, cover, copy ---------------------------------------------------------------


def _result(ctx: OpContext, name: str, a: MonthArg, work) -> Changed:
    month = resolve.month(a.month)
    with ctx.write(name, a), ctx.session() as s:
        message = work(s, month)
        ready = _ready(ctx, s, month)
    return Changed(message=message, ready_to_assign=ready)


def run_targets(ctx: OpContext, a: MonthArg) -> Changed:
    from hiro_budget.services import budget

    def work(s, month: str) -> str:
        r = budget.assign_to_targets(s, ctx.settings, month)
        more = f"; {dot(r.still_needed)} still needed" if r.still_needed else ""
        return f"{month}: {dot(r.amount)} assigned to {r.categories} categories{more}"

    return _result(ctx, "assign_to_targets", a, work)


def run_cover(ctx: OpContext, a: MonthArg) -> Changed:
    from hiro_budget.services import budget

    def work(s, month: str) -> str:
        r = budget.cover_overspending(s, ctx.settings, month)
        more = f"; {dot(r.still_needed)} could not be covered" if r.still_needed else ""
        return f"{month}: {dot(r.amount)} assigned to cover {r.categories} categories{more}"

    return _result(ctx, "cover_overspending", a, work)


def run_copy(ctx: OpContext, a: MonthArg) -> Changed:
    from hiro_budget.services import budget

    def work(s, month: str) -> str:
        n = budget.copy_last_month(s, ctx.settings, month)
        return f"{month}: last month's amounts copied to {n} categories"

    return _result(ctx, "copy_last_month", a, work)


def _op(name, cli, summary, description, args, run) -> Operation:
    return Operation(
        name=name,
        cli=cli,
        summary=summary,
        description=description,
        args=args,
        result=Changed,
        run=run,
        render=render_changed,
        read_only=False,
    )


OPERATIONS = [
    _op(
        "assign_budget",
        ("budget", "assign"),
        "Assign money to a category in a month",
        "Set the amount assigned to a category (or, with relative, add to it). The result "
        "shows the new Ready to Assign. Categories of a group budgeted as a whole take "
        "money on the group: use group:<key>.",
        AssignArgs,
        run_assign,
    ),
    _op(
        "move_money",
        ("budget", "move"),
        "Move assigned money between categories",
        f"Move assigned money from one category to another; '{READY}' stands for Ready to "
        "Assign. The source must have the amount assigned in that month.",
        MoveArgs,
        run_move,
    ),
    _op(
        "assign_to_targets",
        ("budget", "targets"),
        "Fund the targets of underfunded categories",
        "Assign what the targets still need, in budget order, as far as Ready to Assign allows.",
        MonthArg,
        run_targets,
    ),
    _op(
        "cover_overspending",
        ("budget", "cover"),
        "Cover overspent categories from Ready to Assign",
        "Assign money to overspent categories until they are back to zero, as far as Ready "
        "to Assign allows.",
        MonthArg,
        run_cover,
    ),
    _op(
        "copy_last_month",
        ("budget", "copy"),
        "Copy last month's assignments",
        "Assign last month's amounts to the categories that have nothing assigned yet.",
        MonthArg,
        run_copy,
    ),
]
