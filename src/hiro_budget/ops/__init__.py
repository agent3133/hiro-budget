"""The operations layer (#83): everything a user or an agent can do, defined once.

An ``Operation`` has pydantic arguments and a pydantic result, a function and a text
renderer. The command line (``ops.cli``) and the MCP server generate their commands/tools
from ``registry()``, so the two cannot drift apart. Both use only ``call()``.

The operation modules import services only inside ``run``: ``hiro-budget --help`` must not
load numpy, pdfplumber, openpyxl or Alembic (``test_startup.py``).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

if TYPE_CHECKING:
    from hiro_budget.app import App
    from hiro_budget.config import Settings


class OpError(Exception):
    """A user-facing error: unknown category, a booking that cannot be edited, ..."""


class Args(BaseModel):
    """Base of all argument models: unknown fields are errors (typos must not pass)."""

    model_config = ConfigDict(extra="forbid")


class Result(BaseModel):
    """Base of all result models."""


class Changed(Result):
    """The result of a write: what was done, line by line."""

    message: str
    details: list[str] = []
    ids: list[int] = []
    ready_to_assign: str | None = None  # after budget changes, euros
    warnings: list[str] = []

    @model_validator(mode="after")
    def _warn_negative_ready_to_assign(self) -> Changed:
        """The write went through (as in the GUI), but say that more is assigned than there is
        money: an agent otherwise reads a plain success (#89)."""
        r = self.ready_to_assign
        if r is not None and r.startswith("-") and r.strip("-0.,") != "":
            self.warnings.append(
                f"Ready to Assign is negative ({r}): more is assigned than there is money. "
                "Undo the change or move money back."
            )
        return self


def render_changed(r: Changed, width: int | None = None) -> str:
    from hiro_budget.ops.text import eur

    lines = [r.message, *(f"  {d}" for d in r.details)]
    if r.ready_to_assign is not None:
        lines.append(f"Ready to Assign: {eur(r.ready_to_assign)}")
    lines += [f"Warning: {w}" for w in r.warnings]
    return "\n".join(lines)


@dataclass
class OpContext:
    """What an operation runs on: the app (settings, database) and who is calling."""

    app: App
    source: str = "cli"  # "cli" | "mcp"
    session_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    journaling: bool = field(default=False, repr=False)  # inside a journaled write
    backed_up: bool = field(default=False, repr=False)  # the backup before the first write

    @property
    def settings(self) -> Settings:
        return self.app.settings

    def session(self):
        """``with ctx.session() as s:`` - commits on success, rolls back on error."""
        return self.app.session()

    @contextmanager
    def write(self, name: str, args: BaseModel | None = None) -> Iterator[None]:
        """Every write runs inside this (``name``: the operation): the journal (#86) records
        what changed. The backup (#85) hooks in here as well."""
        from hiro_budget.ops import journal

        if name != "import_files":  # an import makes its own backup
            self.backup_once()
        with journal.record(self, name, args):
            yield

    def backup_once(self) -> None:
        """A backup before the first write of this session (not for reads). If it fails,
        nothing is written."""
        if self.backed_up or not self.settings.app.backup.before_write:
            return
        from hiro_budget.services.backup import create_backup

        try:
            create_backup(self.settings, self.source)
        except Exception as e:
            raise OpError(f"the backup before the change failed, nothing was changed: {e}") from e
        self.backed_up = True


@dataclass(frozen=True)
class Operation:
    name: str  # MCP tool name, snake_case
    cli: tuple[str, ...]  # command path: ("tx", "list")
    summary: str  # one line: CLI help, start of the MCP description
    description: str  # for MCP: what it does, units, how ids are found
    args: type[Args]
    result: type[Result]
    run: Callable[[OpContext, Any], Result]
    # text for people and LLMs: render(result, width); width None: nothing is cut (MCP, pipes)
    render: Callable[[Any, int | None], str]
    read_only: bool
    undoable: bool = False  # set by the journal (#86) for journaled writes


def registry() -> list[Operation]:
    """All operations in a stable order. The modules are imported by name in plain
    ``import`` statements: the exe build (Nuitka) finds them only that way."""
    from dataclasses import replace

    from hiro_budget.ops import (
        accounts,
        bookings,
        bookings_write,
        budget,
        budget_write,
        categories,
        categories_write,
        history,
        imports,
        maintenance,
        overview,
        reports,
        rules,
        rules_write,
    )
    from hiro_budget.ops.journal import is_undoable

    ops: list[Operation] = []
    for module in (
        overview,
        accounts,
        budget,
        bookings,
        categories,
        rules,
        reports,
        imports,
        bookings_write,
        categories_write,
        rules_write,
        budget_write,
        maintenance,
        history,
    ):
        ops.extend(
            replace(op, undoable=not op.read_only and is_undoable(op.name))
            for op in module.OPERATIONS
        )
    return ops


def find(name: str) -> Operation:
    for op in registry():
        if op.name == name:
            return op
    raise OpError(f"unknown operation '{name}'")


def _validation_message(e: ValidationError) -> str:
    parts = []
    for err in e.errors():
        where = ".".join(str(p) for p in err["loc"])
        msg = err["msg"].removeprefix("Value error, ")
        parts.append(f"{where}: {msg}" if where else msg)
    return "; ".join(parts)


def call(ctx: OpContext, name: str, raw_args: dict[str, Any] | None = None) -> Result:
    """Validate the arguments, run the operation, turn service errors into ``OpError``.
    The one entry point of the CLI and of MCP."""
    from sqlalchemy.exc import NoResultFound

    op = find(name)
    try:
        args = op.args.model_validate(raw_args or {})
    except ValidationError as e:
        raise OpError(f"invalid arguments: {_validation_message(e)}") from e
    try:
        return op.run(ctx, args)
    except OpError:
        raise
    except NoResultFound as e:
        raise OpError(str(e) or "not found") from e
    except ValueError as e:  # SplitError, AccountError and parsing errors are ValueErrors
        raise OpError(str(e)) from e
    except Exception as e:
        from hiro_budget.config import ConfigError

        if isinstance(e, ConfigError):
            raise OpError(str(e)) from e
        raise


def positional(**extra: Any) -> dict[str, Any]:
    """``Field(json_schema_extra=positional())``: a positional command line argument."""
    return {"cli": "positional", **extra}


def flag(*names: str) -> dict[str, Any]:
    """``Field(json_schema_extra=flag("--from"))``: command line flag other than the name."""
    return {"flags": list(names)}
