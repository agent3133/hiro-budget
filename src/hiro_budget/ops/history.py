"""Operations: list the journaled changes, undo them (#86)."""

from __future__ import annotations

from pydantic import Field

from hiro_budget.ops import (
    Args,
    Changed,
    OpContext,
    Operation,
    OpError,
    Result,
    positional,
    render_changed,
)
from hiro_budget.ops.text import Column, table


class ListChangesArgs(Args):
    session: str | None = Field(None, description="only the changes of this session id")
    limit: int = Field(20, ge=1, le=500, description="how many changes (newest first)")


class ChangeInfo(Result):
    id: int
    time: str
    source: str = Field(description="cli or mcp")
    session: str
    operation: str
    summary: str
    items: int = Field(description="rows and files the change touched")
    undoable: bool
    undone: bool


class ListChangesResult(Result):
    changes: list[ChangeInfo]


def run_list(ctx: OpContext, a: ListChangesArgs) -> ListChangesResult:
    from sqlalchemy import func, select

    from hiro_budget.db.models import ChangeItem, ChangeLog

    with ctx.session() as s:
        q = select(ChangeLog).order_by(ChangeLog.id.desc()).limit(a.limit)
        if a.session:
            q = q.where(ChangeLog.session_id == a.session)
        logs = list(s.scalars(q))
        counted = s.execute(
            select(ChangeItem.log_id, func.count())
            .where(ChangeItem.log_id.in_([x.id for x in logs]))
            .group_by(ChangeItem.log_id)
        ).all()
        counts = {log_id: n for log_id, n in counted}
        changes = [
            ChangeInfo(
                id=x.id,
                time=f"{x.created_at:%Y-%m-%d %H:%M:%S}",
                source=x.source,
                session=x.session_id,
                operation=x.operation,
                summary=x.summary,
                items=counts.get(x.id, 0),
                undoable=x.undoable,
                undone=x.undone_at is not None,
            )
            for x in logs
        ]
    return ListChangesResult(changes=changes)


def render_list(r: ListChangesResult, width: int | None = None) -> str:
    cols = [
        Column("Id", "r"),
        Column("Time"),
        Column("By"),
        Column("Session"),
        Column("Change", shrink=True),
        Column("Items", "r"),
        Column("State"),
    ]
    rows = [
        [
            str(c.id),
            c.time,
            c.source,
            c.session,
            c.summary,
            str(c.items),
            "undone" if c.undone else ("" if c.undoable else "not undoable"),
        ]
        for c in r.changes
    ]
    return table(cols, rows, width)


class UndoArgs(Args):
    id: int | None = Field(
        None,
        description="id of the change (from list_changes)",
        json_schema_extra=positional(nargs="?"),
    )
    session: str | None = Field(None, description="undo all changes of this session, newest first")
    last: bool = Field(False, description="undo the newest change that can be undone")


def _pick(ctx: OpContext, a: UndoArgs) -> list[int]:
    from sqlalchemy import select

    from hiro_budget.db.models import ChangeLog

    given = sum([a.id is not None, a.session is not None, a.last])
    if given != 1:
        raise OpError("give exactly one of: a change id, --session, --last")
    with ctx.session() as s:
        if a.id is not None:
            return [a.id]
        open_ = select(ChangeLog.id).where(
            ChangeLog.undoable.is_(True), ChangeLog.undone_at.is_(None)
        )
        if a.session:
            ids = list(
                s.scalars(
                    open_.where(ChangeLog.session_id == a.session).order_by(ChangeLog.id.desc())
                )
            )
            if not ids:
                raise OpError(f"nothing to undo in session '{a.session}'")
            return ids
        last = s.scalar(open_.order_by(ChangeLog.id.desc()).limit(1))
        if last is None:
            raise OpError("nothing to undo")
        return [last]


def run_undo(ctx: OpContext, a: UndoArgs) -> Changed:
    from hiro_budget.ops import journal

    ids = _pick(ctx, a)
    with ctx.write("undo_change", a):
        result = journal.undo_entries(ctx, ids)
    details = [f"{result.rows} rows restored"] if result.rows else []
    details += [f"{name} restored" for name in result.files]
    return Changed(message=f"undone: change {', '.join(str(i) for i in ids)}", details=details)


OPERATIONS = [
    Operation(
        name="list_changes",
        cli=("history",),
        summary="List the changes made by the CLI and agents",
        description=(
            "The journal of writes (newest first): id, time, who (cli or mcp), the session, "
            "what was done, how many rows/files it touched and whether it can be undone. "
            "Journal entries are kept for 90 days."
        ),
        args=ListChangesArgs,
        result=ListChangesResult,
        run=run_list,
        render=render_list,
        read_only=True,
    ),
    Operation(
        name="undo_change",
        cli=("undo",),
        summary="Undo a change, all changes of a session, or the last change",
        description=(
            "Put back what a journaled change modified: bookings, splits, budget, categories "
            "and the configuration files. Refused, and nothing changed, if something the "
            "change touched was modified since; undo newer changes first. Imports and "
            "training cannot be undone (restore a backup). Undoing a session takes its "
            "changes back newest first."
        ),
        args=UndoArgs,
        result=Changed,
        run=run_undo,
        render=render_changed,
        read_only=False,
    ),
]
