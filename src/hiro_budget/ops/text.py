"""Plain text for terminals and LLMs: amounts, dates, aligned tables.

No decorative box characters and no colour here: the same text is what an MCP client
reads. ``cli`` adds colour on a capable terminal afterwards.
"""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from datetime import date

from hiro_budget.money import format_cents


def dot(cents: int) -> str:
    """Euros as a decimal string with a dot, exact: ``-12.34`` (results of operations)."""
    sign = "-" if cents < 0 else ""
    euros, rest = divmod(abs(cents), 100)
    return f"{sign}{euros}.{rest:02d}"


def dot_or_none(cents: int | None) -> str | None:
    return None if cents is None else dot(cents)


def money(cents: int) -> str:
    """German display format without currency: ``-1.234,56``."""
    return format_cents(cents, currency=None)


def day(d: date | None) -> str:
    return f"{d:%d.%m.%Y}" if d else ""


def terminal_width() -> int | None:
    """Columns of the terminal; None when the output is not a terminal (pipe, MCP)."""
    if not sys.stdout.isatty():
        return None
    return shutil.get_terminal_size((100, 24)).columns


def use_colour() -> bool:
    return sys.stdout.isatty() and "NO_COLOR" not in os.environ


@dataclass(frozen=True)
class Column:
    header: str
    align: str = "l"  # "l" or "r"
    shrink: bool = False  # may be cut with an ellipsis when the terminal is narrow


def _cut(text: str, width: int) -> str:
    return text if len(text) <= width else text[: max(width - 1, 0)] + "…"


def table(columns: list[Column], rows: list[list[str]], width: int | None = None) -> str:
    """Aligned columns separated by two spaces. With ``width``, the widest shrinkable
    column is cut first until the lines fit; without it, nothing is cut."""
    if not rows:
        return "(none)"
    widths = [max(len(c.header), *(len(r[i]) for r in rows)) for i, c in enumerate(columns)]
    if width is not None:
        gaps = 2 * (len(columns) - 1)
        while sum(widths) + gaps > width:
            candidates = [i for i, c in enumerate(columns) if c.shrink and widths[i] > 8]
            if not candidates:
                break
            widest = max(candidates, key=lambda i: widths[i])
            widths[widest] -= 1

    def line(cells: list[str]) -> str:
        out = []
        for i, c in enumerate(columns):
            cell = _cut(cells[i], widths[i])
            out.append(cell.rjust(widths[i]) if c.align == "r" else cell.ljust(widths[i]))
        return "  ".join(out).rstrip()

    head = line([c.header for c in columns])
    rule = "-" * min(len(head), width) if width else "-" * len(head)
    return "\n".join([head, rule, *(line(r) for r in rows)])


def gap_text(account: str, gap) -> str:
    """A statement gap: the period between two statements that do not connect."""
    return (
        f"{account}: statements missing between {gap.after.period_end:%Y-%m-%d} and "
        f"{gap.before.period_start:%Y-%m-%d} ({money(gap.difference_cents)})"
    )


def eur(text: str | None) -> str:
    """A result string ('-12.34') in German display format; '-' for None."""
    return "-" if text is None else money(round(float(text) * 100))


def page_note(shown: int, total: int, offset: int) -> str:
    """The line below a paged list."""
    if total == 0:
        return "0 results"
    end = offset + shown
    note = f"{offset + 1}-{end} of {total}"
    if end < total:
        note += f" (more: --offset {end})"
    return note
