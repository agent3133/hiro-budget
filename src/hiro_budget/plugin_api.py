"""What plugin code may use (plugin API version 2, see ``importers.plugins``).

Plugins import from this module only (and from third-party libraries such as pydantic);
everything else in the app may change. A plugin declares ``api: 2`` in its ``plugin.yaml``.

Everything that brings bookings in returns a ``ParsedStatement``; the app then checks the
balance, finds duplicates and overlaps, imports and categorizes. There are two kinds.

A **file importer** (``importers:`` in the manifest) reads one kind of statement file::

    class MyBankPdfImporter:
        name = "mybank_pdf"   # stored with every import: never change it

        def detect(self, path: Path) -> bool:   # cheap: is this file mine?
            ...

        def parse(self, path: Path) -> ParsedStatement:   # raise ImportError_ if not
            ...

A **connector** (``connectors:``) fetches from a bank (see ``Connector``): settings models
the app renders as a form, and ``connect(login, options, secret, confirm)``, a context
manager whose session lists the bank's accounts (``RemoteAccount``) and fetches each as an
``incremental`` statement. It raises ``LoginError``, ``Offline``, ``Cancelled`` or
``ConnectorError``; the app asks for the secret (PIN) and the confirmations (TAN, app).

A **frontend** (``frontends:``, API 3) adds a command to the command line, e.g.
``hiro-budget mcp``::

    class MyFrontend:
        command = "mine"                  # hiro-budget mine ...
        help = "what it does"
        source = "mine"                   # who a change is journaled as (letters, up to 10)

        def add_arguments(self, parser: argparse.ArgumentParser) -> None: ...
        def run(self, args: argparse.Namespace, operations: Operations) -> int: ...

``Operations`` is everything a frontend may do: ``list()`` the operations of the app (name,
description, JSON schemas of arguments and result, read only / undoable) and ``call(name,
arguments)``. A call returns the text, the data and the error of the operation; it never
raises for what a user or an agent can get wrong.

Code of another plugin (one in ``requires``) is reached with ``module("plugin", "module")``.
"""

from __future__ import annotations

import logging
import types
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from hiro_budget.importers.base import (
    FileImporter,
    ImportError_,
    ParsedHolding,
    ParsedLink,
    ParsedStatement,
    ParsedTransaction,
)
from hiro_budget.importers.connectors import (
    Cancelled,
    Confirmation,
    Connector,
    ConnectorError,
    ConnectorSession,
    LoginError,
    Offline,
    RemoteAccount,
)
from hiro_budget.money import parse_german_amount

API_VERSION = 3

if TYPE_CHECKING:
    from hiro_budget.ops import OpContext

log = logging.getLogger(__name__)

__all__ = [
    "API_VERSION",
    "CallResult",
    "OperationInfo",
    "Operations",
    "app_version",
    "Cancelled",
    "Confirmation",
    "Connector",
    "ConnectorError",
    "ConnectorSession",
    "FileImporter",
    "ImportError_",
    "LoginError",
    "Offline",
    "ParsedHolding",
    "ParsedLink",
    "ParsedStatement",
    "ParsedTransaction",
    "RemoteAccount",
    "enricher_for",
    "module",
    "parse_german_amount",
    "pdf_first_page_text",
    "pdf_page_texts",
]


def app_version() -> str:
    from hiro_budget import __version__

    return __version__


@dataclass(frozen=True)
class OperationInfo:
    name: str  # snake_case, e.g. "list_bookings"
    summary: str  # one line
    description: str  # what it does, units, how ids are found
    input_schema: dict[str, Any]  # JSON schema of the arguments, without $ref
    output_schema: dict[str, Any]  # JSON schema of ``CallResult.data``
    read_only: bool
    undoable: bool  # a change that ``undo_change`` takes back


@dataclass
class CallResult:
    text: str = ""  # what to show a person or an LLM; never cut to a terminal width
    data: dict[str, Any] = field(default_factory=dict)  # the result as JSON
    error: str | None = None  # a message the caller can act on; then text is the same


class Operations:
    """The operations of the app for a frontend. One per run: its ``session_id`` groups
    the changes it makes (``undo_change`` takes a whole session back)."""

    def __init__(self, ctx: OpContext, read_only: bool = False) -> None:
        self._ctx = ctx
        self.read_only = read_only  # only the operations that do not change anything

    @property
    def session_id(self) -> str:
        return self._ctx.session_id

    def list(self) -> list[OperationInfo]:
        from hiro_budget.ops import registry
        from hiro_budget.ops.schema import inline_schema

        return [
            OperationInfo(
                name=op.name,
                summary=op.summary,
                description=op.description,
                input_schema=inline_schema(op.args),
                output_schema=inline_schema(op.result, mode="serialization"),
                read_only=op.read_only,
                undoable=op.undoable,
            )
            for op in registry()
            if op.read_only or not self.read_only
        ]

    def call(self, name: str, arguments: dict[str, Any] | None = None) -> CallResult:
        from hiro_budget.ops import OpError, call, find

        try:
            op = find(name)
            if self.read_only and not op.read_only:
                raise OpError(f"'{name}' changes data and this server is read only")
            result = call(self._ctx, name, arguments or {})
            return CallResult(text=op.render(result, None), data=result.model_dump(mode="json"))
        except OpError as e:
            return CallResult(text=str(e), error=str(e))
        except Exception as e:  # a bug, not the caller's mistake: logged, the caller goes on
            log.exception("operation %s failed", name)
            message = f"internal error in {name}: {type(e).__name__}: {e}"
            return CallResult(text=message, error=message)

    @classmethod
    def create(cls, settings: Any, source: str, read_only: bool = False) -> Operations:
        """For the app: the operations on these settings, changes journaled as ``source``."""
        from hiro_budget.app import App
        from hiro_budget.ops import OpContext

        return cls(OpContext(App(settings), source=source), read_only)


def pdf_page_texts(path: Path, x_tolerance: float = 3) -> list[str]:
    """The text of every page as pdfplumber extracts it (``x_tolerance``: how far apart
    characters may be and still form one word)."""
    import pdfplumber

    with pdfplumber.open(path) as pdf:
        return [page.extract_text(x_tolerance=x_tolerance) or "" for page in pdf.pages]


def pdf_first_page_text(path: Path, x_tolerance: float = 3) -> str:
    """The first page's text, for ``detect``; empty if the file is no readable PDF."""
    import pdfplumber

    try:
        with pdfplumber.open(path) as pdf:
            if not pdf.pages:
                return ""
            return pdf.pages[0].extract_text(x_tolerance=x_tolerance) or ""
    except Exception:  # not a readable PDF
        return ""


def module(plugin: str, name: str) -> types.ModuleType:
    """Module ``name`` of the available plugin ``plugin`` (a replaced plugin: the user's)."""
    from hiro_budget.importers.plugins import current

    return current().module(plugin, name)


def enricher_for(*, bank_code: str | None = None, institution: str | None = None):
    """The function ``(counterparty, purpose, booking_date) -> (merchant, purchase_date)``
    a plugin registered for this bank, or None."""
    from hiro_budget.importers.plugins import current

    return current().enricher_for(bank_code=bank_code, institution=institution)
