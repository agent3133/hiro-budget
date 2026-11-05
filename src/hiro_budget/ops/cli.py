"""The command line, generated from the operations registry.

Every argument model field becomes an option (or a positional argument, marked with
``positional()``). Values reach pydantic as given, so the command line and MCP validate
the same way (``ops.call``).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import types
import typing
from typing import TYPE_CHECKING, Any, Literal

from hiro_budget.ops import OpContext, Operation, OpError, call, registry

if TYPE_CHECKING:
    from hiro_budget.config import Settings

EXIT_OK, EXIT_ERROR, EXIT_USAGE = 0, 1, 2
_DEST_OP = "_operation"
_NEGATIVE_AMOUNT = re.compile(r"^-\d[\d.,]*$")


def _unwrap(annotation: Any) -> tuple[Any, bool]:
    """(base type, is a list): ``list[str] | None`` -> (str, True)."""
    origin = typing.get_origin(annotation)
    if origin in (typing.Union, types.UnionType):
        inner = [a for a in typing.get_args(annotation) if a is not type(None)]
        return _unwrap(inner[0]) if inner else (str, False)
    if origin is list:
        return typing.get_args(annotation)[0], True
    return annotation, False


def _flags(name: str, extra: dict[str, Any], default: Any, is_bool: bool) -> list[str]:
    if extra.get("flags"):
        return list(extra["flags"])
    kebab = name.replace("_", "-")
    return [f"--no-{kebab}" if is_bool and default is True else f"--{kebab}"]


def _add_field(parser: argparse.ArgumentParser, name: str, field: Any) -> None:
    base, is_list = _unwrap(field.annotation)
    extra = field.json_schema_extra if isinstance(field.json_schema_extra, dict) else {}
    help_text = field.description or ""
    if extra.get("cli") == "positional":
        kw: dict[str, Any] = {"help": help_text, "metavar": name.upper()}
        if is_list:
            kw["nargs"] = "+" if field.is_required() else "*"
        elif extra.get("nargs"):
            kw["nargs"] = extra["nargs"]
        if "nargs" in kw and kw["nargs"] in ("?", "*"):
            kw["default"] = argparse.SUPPRESS
        parser.add_argument(name, **kw)
        return
    is_bool = base is bool
    flags = _flags(name, extra, field.default, is_bool)
    kw = {"dest": name, "help": help_text, "default": argparse.SUPPRESS}
    if is_bool:
        kw["action"] = "store_false" if field.default is True else "store_true"
    else:
        if typing.get_origin(base) is Literal:
            kw["choices"] = list(typing.get_args(base))
        if is_list:  # --part a b  and  --part a --part b both work
            kw["nargs"] = "+"
            kw["action"] = "extend"
        if field.is_required():
            kw["required"] = True
    parser.add_argument(*flags, **kw)


def add_commands(sub: argparse._SubParsersAction) -> None:
    """One (nested) subparser per operation."""
    groups: dict[tuple[str, ...], Any] = {}
    for op in registry():
        path = op.cli
        if not path:  # import, recategorize, train: the commands of __main__
            continue
        for depth in range(1, len(path)):
            key = path[:depth]
            if key not in groups:
                parent = sub if depth == 1 else groups[path[: depth - 1]]
                p = parent.add_parser(path[depth - 1], help=f"{path[depth - 1]} commands")
                groups[key] = p.add_subparsers(
                    dest=f"_cmd{depth}", metavar="COMMAND", required=True
                )
        parent = sub if len(path) == 1 else groups[path[:-1]]
        leaf = parent.add_parser(
            path[-1], help=op.summary, description=f"{op.summary}.\n\n{op.description}"
        )
        leaf.set_defaults(**{_DEST_OP: op})
        # "-12,50" and "-1.234,56" are amounts, not options (argparse knows only "-12.5")
        leaf._negative_number_matcher = _NEGATIVE_AMOUNT
        for name, field in op.args.model_fields.items():
            _add_field(leaf, name, field)
        add_json_option(leaf)


def add_json_option(parser: argparse.ArgumentParser) -> None:
    """``--json`` after a command; ``SUPPRESS``: it does not reset a ``--json`` given before
    the command (the program's own option, ``__main__``)."""
    parser.add_argument(
        "--json",
        dest="json_output",
        action="store_true",
        default=argparse.SUPPRESS,
        help="print the result as JSON",
    )


def print_result(op: Operation, result: Any, as_json: bool) -> None:
    from hiro_budget.ops import text

    if as_json:
        print(json.dumps(result.model_dump(mode="json"), indent=2, ensure_ascii=False))
        return
    out = op.render(result, text.terminal_width())
    print(colour(out) if text.use_colour() else out)


def op_args(op: Operation, ns: argparse.Namespace) -> dict[str, Any]:
    return {name: getattr(ns, name) for name in op.args.model_fields if hasattr(ns, name)}


# --- terminal --------------------------------------------------------------------------


def prepare_console() -> None:
    """UTF-8 output (the Windows console defaults to a code page that breaks umlauts and
    ``€``) and, on Windows, ANSI colour support."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")
    if sys.platform == "win32" and sys.stdout.isatty():
        try:
            import ctypes

            kernel = ctypes.windll.kernel32
            handle = kernel.GetStdHandle(-11)
            mode = ctypes.c_uint32()
            if kernel.GetConsoleMode(handle, ctypes.byref(mode)):
                kernel.SetConsoleMode(handle, mode.value | 0x0004)  # VIRTUAL_TERMINAL_PROCESSING
        except (OSError, AttributeError):
            pass


_NEGATIVE = re.compile(r"(?<![\w.,])-\d[\d.]*,\d\d\b")


def colour(text: str) -> str:
    """Negative amounts in red."""
    return _NEGATIVE.sub(lambda m: f"\x1b[31m{m.group(0)}\x1b[0m", text)


def run_operation(settings: Settings, ns: argparse.Namespace) -> int:
    op: Operation = getattr(ns, _DEST_OP)
    return run_named(settings, op.name, op_args(op, ns), getattr(ns, "json_output", False))


def run_named(settings: Settings, name: str, args: dict[str, Any], as_json: bool) -> int:
    """Run one operation for the command line and print its result (text or JSON)."""
    import logging

    from hiro_budget.app import App
    from hiro_budget.ops import find

    prepare_console()
    logging.getLogger().setLevel(logging.WARNING)  # no migration chatter between the tables
    ctx = OpContext(App(settings), source="cli")
    try:
        result = call(ctx, name, args)
    except OpError as e:
        print(f"Error: {e}", file=sys.stderr)
        return EXIT_ERROR
    print_result(find(name), result, as_json)
    return EXIT_OK
