"""The change journal (#86): every write is recorded so that it can be undone.

How: before a write, the journaled tables and the three YAML files are read; after it,
again. The differences (a row before and after, a file before and after) go into
``change_log`` / ``change_item``. Comparing snapshots instead of hooking the ORM sees
everything a write does, also what the database does by itself (``ON DELETE SET NULL`` of
linked payments, cascades) and any statement that bypasses the ORM.

Undo puts rows and files back to their "before", but only if they are still exactly as
the change left them; otherwise nothing is touched.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from hiro_budget.ops import OpError

if TYPE_CHECKING:
    from sqlalchemy import Table
    from sqlalchemy.orm import Session

    from hiro_budget.ops import OpContext

# Parents before children (insertion order of an undo).
TABLES = (
    "account",
    "category_group",
    "category",
    "transaction",
    "split",
    "linked_payment",
    "budget_assignment",
)
FILES = ("config.yaml", "categories.yaml", "rules.yaml")
KEEP_DAYS = 90  # older journal entries are deleted with the next write
# Operations whose effect is not a set of journaled rows: the summary is kept, undo points
# to the backup.
NOT_UNDOABLE = frozenset({"import_files", "train", "undo_change"})

Row = dict[str, Any]


def _table(name: str) -> Table:
    from hiro_budget.db.models import Base

    return Base.metadata.tables[name]


@dataclass
class Snapshot:
    rows: dict[str, dict[int, Row]] = field(default_factory=dict)
    files: dict[str, str | None] = field(default_factory=dict)


@dataclass
class Item:
    kind: str  # "row" | "file"
    name: str  # table name or file name
    row_id: int | None
    before: Any  # a row (dict) or, for a file, its text; None: did not exist
    after: Any


def read_files(ctx: OpContext) -> dict[str, str | None]:
    files: dict[str, str | None] = {}
    for name in FILES:
        path = ctx.settings.config_dir / name
        # bytes: a file comes back with the very line endings it had
        files[name] = path.read_bytes().decode("utf-8") if path.exists() else None
    return files


def snapshot(ctx: OpContext) -> Snapshot:
    from sqlalchemy import select

    snap = Snapshot(files=read_files(ctx))
    with ctx.app.session_factory() as s:
        for name in TABLES:
            snap.rows[name] = {r.id: dict(r._mapping) for r in s.execute(select(_table(name)))}
    return snap


def diff(before: Snapshot, after: Snapshot) -> list[Item]:
    items: list[Item] = []
    for name in TABLES:
        old, new = before.rows.get(name, {}), after.rows.get(name, {})
        for row_id in sorted(old.keys() | new.keys()):
            a, b = old.get(row_id), new.get(row_id)
            if a != b:
                items.append(Item("row", name, row_id, a, b))
    for name in FILES:
        old_text, new_text = before.files.get(name), after.files.get(name)
        if old_text != new_text:
            items.append(Item("file", name, None, old_text, new_text))
    return items


# --- JSON <-> row ------------------------------------------------------------------------


def _encode_value(value: Any) -> Any:
    if isinstance(value, dt.datetime | dt.date):
        return value.isoformat()
    return str(value) if hasattr(value, "value") else value  # enum members


def encode_row(row: Row | None) -> str | None:
    if row is None:
        return None
    return json.dumps({k: _encode_value(v) for k, v in row.items()}, ensure_ascii=False)


def decode_row(table_name: str, text: str | None) -> Row | None:
    """The row as the table's columns want it (dates are dates again)."""
    import sqlalchemy as sa

    if text is None:
        return None
    table = _table(table_name)
    row = json.loads(text)
    for key, value in row.items():
        if value is None:
            continue
        kind = table.c[key].type
        if isinstance(kind, sa.DateTime):
            row[key] = dt.datetime.fromisoformat(value)
        elif isinstance(kind, sa.Date):
            row[key] = dt.date.fromisoformat(value)
    return row


# --- recording ---------------------------------------------------------------------------


def _args_json(args: BaseModel | None) -> str:
    return json.dumps(args.model_dump(mode="json", exclude_none=True) if args else {}, default=str)


def _summary(name: str, args: BaseModel | None) -> str:
    from hiro_budget.ops import find

    given = args.model_dump(mode="json", exclude_none=True, exclude_defaults=True) if args else {}
    shown = ", ".join(f"{k}={v}" for k, v in given.items())
    text = f"{find(name).summary}" + (f" ({shown})" if shown else "")
    return text if len(text) <= 300 else text[:299] + "…"


def is_undoable(name: str) -> bool:
    return name not in NOT_UNDOABLE


def _store(
    ctx: OpContext, name: str, args: BaseModel | None, items: list[Item], undoable: bool
) -> int:
    from sqlalchemy import insert

    from hiro_budget.db.models import ChangeItem, ChangeLog

    with ctx.session() as s:
        log = ChangeLog(
            session_id=ctx.session_id,
            source=ctx.source,
            operation=name,
            args_json=_args_json(args),
            summary=_summary(name, args),
            undoable=undoable,
        )
        s.add(log)
        s.flush()
        if items:
            s.execute(
                insert(ChangeItem),
                [
                    {
                        "log_id": log.id,
                        "kind": i.kind,
                        "table_name": i.name if i.kind == "row" else None,
                        "row_id": i.row_id,
                        "path": i.name if i.kind == "file" else None,
                        "before_json": i.before if i.kind == "file" else encode_row(i.before),
                        "after_json": i.after if i.kind == "file" else encode_row(i.after),
                    }
                    for i in items
                ],
            )
        _prune(s)
        return log.id


def _prune(s: Session) -> None:
    from sqlalchemy import delete

    from hiro_budget.db.models import ChangeLog

    s.execute(
        delete(ChangeLog).where(
            ChangeLog.created_at < dt.datetime.now() - dt.timedelta(days=KEEP_DAYS)
        )
    )


@contextmanager
def record(ctx: OpContext, name: str, args: BaseModel | None) -> Iterator[None]:
    """Journal the changes made inside the ``with`` block as one entry of operation ``name``.
    A block that fails is journaled too when it changed something (the entry can be undone)."""
    if ctx.journaling:  # nested: the outer block records it all
        yield
        return
    ctx.journaling = True
    undoable = is_undoable(name)
    before = snapshot(ctx) if undoable else Snapshot(files=read_files(ctx))
    failed = False
    try:
        yield
    except BaseException:
        failed = True
        raise
    finally:
        ctx.journaling = False
        after = snapshot(ctx) if undoable else Snapshot(files=read_files(ctx))
        items = diff(before, after)
        if items or not failed:
            _store(ctx, name, args, items, undoable)


# --- undo --------------------------------------------------------------------------------


@dataclass
class UndoResult:
    entries: list[int]
    rows: int
    files: list[str]


def _current_row(s: Session, table_name: str, row_id: int) -> Row | None:
    from sqlalchemy import select

    table = _table(table_name)
    r = s.execute(select(table).where(table.c.id == row_id)).first()
    return dict(r._mapping) if r else None


def _same(current: Row | None, expected: Row | None) -> bool:
    return current == expected


def _item_label(table_name: str, row_id: int) -> str:
    return f"{table_name} {row_id}"


def undo_entries(ctx: OpContext, log_ids: list[int]) -> UndoResult:
    """Undo the entries in the order given (newest first). All or nothing."""
    from sqlalchemy import delete, insert, select, text, update

    from hiro_budget.db.models import ChangeItem, ChangeLog

    virtual: dict[str, str | None] = read_files(ctx)
    start_files = dict(virtual)
    rows_restored = 0
    with ctx.session() as s:
        s.execute(text("PRAGMA defer_foreign_keys=ON"))
        for log_id in log_ids:
            log = s.get(ChangeLog, log_id)
            if log is None:
                raise OpError(f"no change {log_id}")
            if not log.undoable:
                raise OpError(
                    f"change {log_id} ({log.operation}) cannot be undone; "
                    "restore a backup (Settings -> data folder -> backups)"
                )
            if log.undone_at is not None:
                raise OpError(f"change {log_id} was undone already")
            items = list(s.scalars(select(ChangeItem).where(ChangeItem.log_id == log_id)))
            rows = [i for i in items if i.kind == "row"]
            conflicts = []
            for i in rows:
                assert i.table_name and i.row_id is not None
                expected = decode_row(i.table_name, i.after_json)
                if not _same(_current_row(s, i.table_name, i.row_id), expected):
                    conflicts.append(_item_label(i.table_name, i.row_id))
            for i in items:
                if i.kind == "file" and virtual.get(i.path or "") != i.after_json:
                    conflicts.append(f"file {i.path}")
            if conflicts:
                shown = ", ".join(conflicts[:8]) + (" ..." if len(conflicts) > 8 else "")
                raise OpError(
                    f"change {log_id} ({log.operation}) cannot be undone: changed since: {shown}"
                )
            # deletions (children first), then insertions and updates (parents first)
            order = {name: n for n, name in enumerate(TABLES)}
            for i in sorted(rows, key=lambda x: -order[x.table_name or ""]):
                if i.before_json is None:
                    table = _table(i.table_name or "")
                    s.execute(delete(table).where(table.c.id == i.row_id))
            for i in sorted(rows, key=lambda x: (order[x.table_name or ""], x.row_id or 0)):
                if i.before_json is None:
                    continue
                table = _table(i.table_name or "")
                before = decode_row(i.table_name or "", i.before_json) or {}
                if i.after_json is None:
                    s.execute(insert(table).values(**before))
                else:
                    values = {k: v for k, v in before.items() if k != "id"}
                    s.execute(update(table).where(table.c.id == i.row_id).values(**values))
            rows_restored += len(rows)
            for i in items:
                if i.kind == "file":
                    virtual[i.path or ""] = i.before_json
            log.undone_at = dt.datetime.now()
    changed_files = [n for n in FILES if virtual[n] != start_files[n]]
    for name in changed_files:
        text_ = virtual[name]
        if text_ is not None:
            (ctx.settings.config_dir / name).write_bytes(text_.encode("utf-8"))
    if changed_files:
        ctx.app.reload_config()
    return UndoResult(log_ids, rows_restored, changed_files)
