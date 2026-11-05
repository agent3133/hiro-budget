"""Operations that change categories and groups (categories.yaml, comments kept)."""

from __future__ import annotations

import difflib
from collections.abc import Callable
from datetime import date
from typing import Literal

from pydantic import Field

from hiro_budget.ops import (
    Args,
    Changed,
    OpContext,
    Operation,
    OpError,
    flag,
    positional,
    render_changed,
)


def _find_group(cfg, text: str):
    wanted = text.strip().lower()
    for g in cfg.groups:
        if wanted in (g.key.lower(), g.name.lower()):
            return g
    names = [g.key for g in cfg.groups]
    close = difflib.get_close_matches(wanted, names, n=3, cutoff=0.5)
    hint = f" Did you mean: {', '.join(close)}?" if close else ""
    raise OpError(f"unknown category group '{text}'.{hint}")


def _find_category(cfg, text: str):
    """(group, category) by full key, exact name or a key that is unique."""
    wanted = text.strip().lower()
    pairs = [(g, c) for g in cfg.groups for c in g.categories]
    for pick in (
        [p for p in pairs if f"{p[0].key}.{p[1].key}".lower() == wanted],
        [p for p in pairs if p[1].name.lower() == wanted],
        [p for p in pairs if p[1].key.lower() == wanted],
    ):
        if len(pick) == 1:
            return pick[0]
        if len(pick) > 1:
            raise OpError(f"'{text}' is ambiguous; use the full key 'group.category'")
    keys = [f"{g.key}.{c.key}" for g, c in pairs]
    close = difflib.get_close_matches(wanted, keys, n=3, cutoff=0.5)
    hint = f" Did you mean: {', '.join(close)}?" if close else ""
    raise OpError(f"unknown category '{text}'.{hint}")


def _edit(
    ctx: OpContext,
    name: str,
    args: Args,
    mutate: Callable[..., str],
    details: list[str] | None = None,
) -> Changed:
    """Change a copy of the categories, write categories.yaml (rules and config references
    follow renames) and push the result into the database as the GUI editor does."""
    from hiro_budget.config import CategoriesConfig
    from hiro_budget.services import config_edit
    from hiro_budget.services.sync import check_key_renames

    with ctx.write(name, args):
        cfg = ctx.settings.categories.model_copy(deep=True)
        renames = config_edit.KeyRenames()
        message = mutate(cfg, renames)
        cfg = CategoriesConfig.model_validate(cfg.model_dump())  # duplicate keys and so on
        if renames:
            with ctx.session() as s:
                check_key_renames(s, renames.groups, renames.categories)
        config_edit.save_categories(ctx.settings, cfg, renames)
        ctx.app.sync_config(renames)
    return Changed(message=message, details=details or [])


def _taken_keys(cfg) -> set[str]:
    return {g.key for g in cfg.groups}


# --- create ---------------------------------------------------------------------------


class CreateCategoryArgs(Args):
    group: str = Field(description="group key or name (see list_categories)")
    name: str = Field(description="display name of the new category")
    key: str | None = Field(None, description="key (default: made from the name)")
    one_off: bool = Field(False, description="irregular: reports can leave it out")


def run_create_category(ctx: OpContext, a: CreateCategoryArgs) -> Changed:
    from hiro_budget.config import CategoryDef
    from hiro_budget.services.config_edit import slugify, unique

    def mutate(cfg, renames) -> str:
        g = _find_group(cfg, a.group)
        same = next(
            (c for c in g.categories if c.name.casefold() == a.name.strip().casefold()), None
        )
        if same is not None:
            raise OpError(
                f"group '{g.key}' already has a category '{same.name}' ({g.key}.{same.key})"
            )
        key = a.key or unique(slugify(a.name), {c.key for c in g.categories})
        if any(c.key == key for c in g.categories):
            raise OpError(f"group '{g.key}' already has a category with the key '{key}'")
        g.categories.append(CategoryDef(key=key, name=a.name, one_off=a.one_off))
        return f"category {g.key}.{key} created"

    return _edit(ctx, "create_category", a, mutate)


class RenameCategoryArgs(Args):
    category: str = Field(description="category key or name", json_schema_extra=positional())
    name: str = Field(description="the new display name", json_schema_extra=positional())
    key: str | None = Field(
        None, description="also change the key (bookings, rules and budget follow)"
    )


def run_rename_category(ctx: OpContext, a: RenameCategoryArgs) -> Changed:
    def mutate(cfg, renames) -> str:
        g, c = _find_category(cfg, a.category)
        old_full = f"{g.key}.{c.key}"
        c.name = a.name
        if a.key and a.key != c.key:
            c.key = a.key
            renames.categories[old_full] = f"{g.key}.{a.key}"
        return f"category {old_full} is now '{a.name}'" + (
            f" ({g.key}.{c.key})" if old_full != f"{g.key}.{c.key}" else ""
        )

    return _edit(ctx, "rename_category", a, mutate)


class HideCategoryArgs(Args):
    category: str = Field(description="category key or name", json_schema_extra=positional())


def run_hide_category(ctx: OpContext, a: HideCategoryArgs) -> Changed:
    def mutate(cfg, renames) -> str:
        g, c = _find_category(cfg, a.category)
        g.categories.remove(c)
        return f"category {g.key}.{c.key} hidden (old bookings keep it)"

    return _edit(ctx, "hide_category", a, mutate)


class CreateGroupArgs(Args):
    name: str = Field(description="display name of the new group")
    key: str | None = Field(None, description="key (default: made from the name)")
    income: bool = Field(False, description="a group of income categories")


def run_create_group(ctx: OpContext, a: CreateGroupArgs) -> Changed:
    from hiro_budget.config import GroupDef
    from hiro_budget.services.config_edit import slugify, unique

    def mutate(cfg, renames) -> str:
        key = a.key or unique(slugify(a.name), _taken_keys(cfg))
        if key in _taken_keys(cfg):
            raise OpError(f"a group with the key '{key}' exists")
        cfg.groups.append(GroupDef(key=key, name=a.name, income=a.income))
        return f"group {key} created"

    return _edit(ctx, "create_group", a, mutate)


class RenameGroupArgs(Args):
    group: str = Field(description="group key or name", json_schema_extra=positional())
    name: str = Field(description="the new display name", json_schema_extra=positional())
    key: str | None = Field(None, description="also change the key")


def run_rename_group(ctx: OpContext, a: RenameGroupArgs) -> Changed:
    def mutate(cfg, renames) -> str:
        g = _find_group(cfg, a.group)
        old = g.key
        g.name = a.name
        if a.key and a.key != old:
            g.key = a.key
            renames.groups[old] = a.key
            for c in g.categories:
                renames.categories[f"{old}.{c.key}"] = f"{a.key}.{c.key}"
        return f"group {old} is now '{a.name}'" + (f" ({g.key})" if old != g.key else "")

    return _edit(ctx, "rename_group", a, mutate)


# --- targets --------------------------------------------------------------------------


class TargetArgs(Args):
    category: str = Field(
        description="category key or name, or group:<key> for a group budgeted as a whole",
        json_schema_extra=positional(),
    )
    type: Literal["monthly", "by_date", "repeating", "balance", "none"] = Field(
        description=(
            "monthly: assign the amount every month; by_date: have it by a date; repeating: "
            "every N months from a date; balance: build up to it; none: remove the target"
        ),
        json_schema_extra=positional(),
    )
    amount: float | None = Field(None, gt=0, description="euros (not for 'none')")
    target_date: date | None = Field(
        None,
        description="YYYY-MM-DD for by_date, repeating, balance",
        json_schema_extra=flag("--date"),
    )
    every_months: int = Field(12, ge=1, le=120, description="for repeating")


def run_target(ctx: OpContext, a: TargetArgs) -> Changed:
    from pydantic import ValidationError

    from hiro_budget.config import TargetDef
    from hiro_budget.services import config_edit

    target = None
    if a.type != "none":
        if a.amount is None:
            raise OpError("a target needs an amount")
        try:
            target = TargetDef(
                type=a.type, amount=a.amount, date=a.target_date, every_months=a.every_months
            )
        except ValidationError as e:
            raise OpError(e.errors()[0]["msg"].removeprefix("Value error, ")) from e
    with ctx.write("set_category_target", a):
        if a.category.startswith("group:"):
            g = _find_group(ctx.settings.categories, a.category.removeprefix("group:"))
            config_edit.save_group_target(ctx.settings, g.key, target)
            what = f"group {g.key}"
        else:
            g, c = _find_category(ctx.settings.categories, a.category)
            config_edit.save_target(ctx.settings, f"{g.key}.{c.key}", target)
            what = f"{g.key}.{c.key}"
    return Changed(message=f"target of {what}: " + ("removed" if target is None else a.type))


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
        "create_category",
        ("categories", "add"),
        "Create a category in a group",
        "Create a category. Check list_categories first so you do not create a duplicate. "
        "The key is made from the name unless given.",
        CreateCategoryArgs,
        run_create_category,
    ),
    _op(
        "rename_category",
        ("categories", "rename"),
        "Rename a category (and optionally its key)",
        "Change the display name of a category; with key also its key. Bookings, rules and "
        "budget follow a key change.",
        RenameCategoryArgs,
        run_rename_category,
    ),
    _op(
        "hide_category",
        ("categories", "hide"),
        "Hide (remove) a category",
        "Remove a category from the configuration. Its old bookings keep it. Refused while "
        "a rule or setting still uses it.",
        HideCategoryArgs,
        run_hide_category,
    ),
    _op(
        "create_group",
        ("groups", "add"),
        "Create a category group",
        "Create a group of categories (income groups hold income categories).",
        CreateGroupArgs,
        run_create_group,
    ),
    _op(
        "rename_group",
        ("groups", "rename"),
        "Rename a category group (and optionally its key)",
        "Change the display name of a group; with key also its key (categories follow).",
        RenameGroupArgs,
        run_rename_group,
    ),
    _op(
        "set_category_target",
        ("categories", "target"),
        "Set or remove the budget target of a category",
        "Set the target of a category (or of a group budgeted as a whole, group:<key>): "
        "monthly, by_date, repeating or balance; type none removes it.",
        TargetArgs,
        run_target,
    ),
]
