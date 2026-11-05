"""Turning what a user types into ids: months, amounts, categories, accounts."""

from __future__ import annotations

import difflib
import re
from datetime import date
from typing import TYPE_CHECKING

from hiro_budget.ops import OpError

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from hiro_budget.db.models import Account, Category, CategoryGroup

_MONTH = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def month(text: str | None) -> str:
    """``YYYY-MM``; None or empty: the current month."""
    if not text:
        return f"{date.today():%Y-%m}"
    if not _MONTH.match(text):
        raise OpError(f"'{text}' is not a month; use YYYY-MM, e.g. 2026-11")
    return text


def month_start(text: str) -> date:
    y, m = month(text).split("-")
    return date(int(y), int(m), 1)


def month_end(text: str) -> date:
    from hiro_budget.services.budget import month_last_day

    return month_last_day(month(text))


def cents(text: str) -> int:
    """An amount as typed: ``-12.34``, ``-12,34``, ``1.234,56``, ``12 €``."""
    from hiro_budget.money import parse_user_amount

    try:
        return parse_user_amount(text)
    except ValueError as e:
        raise OpError(f"'{text}' is not an amount; use e.g. -12.34 or 1.234,56") from e


def _suggest(text: str, names: list[str], shown: dict[str, str] | None = None) -> str:
    """Close matches to ``text``; ``shown`` maps a name to what to print instead (a key)."""
    close = difflib.get_close_matches(text, list(dict.fromkeys(names)), n=5, cutoff=0.5)
    labels = list(dict.fromkeys((shown or {}).get(c, c) for c in close))
    return f" Did you mean: {', '.join(labels)}?" if labels else ""


def category(session: Session, text: str) -> Category:
    """A category by its key (``group.key``, or the key alone when unique) or exact name."""
    from sqlalchemy import select

    from hiro_budget.db.models import Category

    cats = list(session.scalars(select(Category)))
    wanted = text.strip().lower()
    for pick in (
        [c for c in cats if c.key.lower() == wanted],
        [c for c in cats if c.name.lower() == wanted],
        [c for c in cats if c.key.lower().split(".")[-1] == wanted],
    ):
        if len(pick) == 1:
            return pick[0]
        if len(pick) > 1:
            keys = ", ".join(sorted(c.key for c in pick))
            raise OpError(f"'{text}' is ambiguous: {keys}")
    names = [c.key for c in cats] + [c.name for c in cats]
    key_of = {c.name: c.key for c in cats}
    raise OpError(f"unknown category '{text}'.{_suggest(text, names, key_of)}")


def group(session: Session, text: str) -> CategoryGroup:
    from sqlalchemy import select

    from hiro_budget.db.models import CategoryGroup

    groups = list(session.scalars(select(CategoryGroup)))
    wanted = text.strip().lower()
    for g in groups:
        if wanted in (g.key.lower(), g.name.lower()):
            return g
    names = [g.key for g in groups] + [g.name for g in groups]
    raise OpError(f"unknown category group '{text}'.{_suggest(text, names)}")


def account(session: Session, text: str) -> Account:
    """An account by name or IBAN (spaces and case ignored in the IBAN)."""
    from sqlalchemy import select

    from hiro_budget.db.models import Account

    accounts = list(session.scalars(select(Account)))
    wanted = text.strip().lower()
    compact = wanted.replace(" ", "")
    for a in accounts:
        if a.name.lower() == wanted or (a.iban and a.iban.replace(" ", "").lower() == compact):
            return a
    by_part = [a for a in accounts if wanted in a.name.lower()]
    if len(by_part) == 1:
        return by_part[0]
    names = [a.name for a in accounts]
    if len(by_part) > 1:
        raise OpError(f"'{text}' is ambiguous: {', '.join(sorted(a.name for a in by_part))}")
    raise OpError(f"unknown account '{text}'.{_suggest(text, names)}")


def category_filter(session: Session, text: str) -> str | int:
    """The category filter of the bookings list: a special word, ``group:<key>`` or a
    category."""
    from hiro_budget.services import transactions as txs

    if text in (txs.UNCATEGORIZED, txs.REVIEW, txs.TRANSFERS):
        return text
    if text.startswith(txs.GROUP_PREFIX):
        return txs.group_filter(group(session, text.removeprefix(txs.GROUP_PREFIX)).id)
    return category(session, text).id
