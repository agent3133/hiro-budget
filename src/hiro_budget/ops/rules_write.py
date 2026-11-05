"""Operations that change the categorization rules (rules.yaml, comments kept)."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import Field, ValidationError

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
from hiro_budget.ops.rules import COUNTERPARTY_HELP, MERCHANT_HELP, no_match_hint

MATCH_FIELDS = (
    "counterparty",
    "merchant",
    "purpose",
    "booking_type",
    "details",
    "account",
    "amount_min",
    "amount_max",
)


class MatchArgs(Args):
    counterparty: str | None = Field(None, description=COUNTERPARTY_HELP)
    merchant: str | None = Field(None, description=MERCHANT_HELP)
    purpose: str | None = Field(None, description="regex on the purpose")
    booking_type: str | None = Field(None, description="regex on the booking type")
    details: str | None = Field(None, description="regex on the details of a PayPal payment")
    account: str | None = Field(None, description="regex on the account name")
    amount_min: float | None = Field(None, description="smallest amount in euros (signed)")
    amount_max: float | None = Field(None, description="largest amount in euros (signed)")


def _conditions(a: MatchArgs) -> dict:
    conditions = {k: v for k in MATCH_FIELDS if (v := getattr(a, k)) is not None}
    for name, value in conditions.items():
        if isinstance(value, str):
            try:
                re.compile(value)
            except re.error as e:
                raise OpError(f"{name}: not a valid regular expression: {e}") from e
    return conditions


def _apply(ctx: OpContext) -> str:
    from hiro_budget.services.categorization.pipeline import recategorize_all

    with ctx.session() as s:
        stats = recategorize_all(s, ctx.settings)
    return f"categorization: {stats}"


def _warnings(ctx: OpContext, rule) -> list[str]:
    """A rule that matches no booking is accepted, but say so (#90)."""
    from hiro_budget.services.config_edit import rule_hits, rule_samples

    with ctx.session() as s:
        (hits,) = rule_hits(rule_samples(s), [rule])
    return [] if hits.matching else [no_match_hint(rule.match.model_dump(exclude_none=True))]


def _save(ctx: OpContext, rules: list) -> None:
    from hiro_budget.services import config_edit

    config_edit.save_rules(ctx.settings, rules)


def _rule(rule_id: str, category: str, conditions: dict):
    from hiro_budget.config import RuleDef, RuleMatch

    try:
        return RuleDef(id=rule_id, category=category, match=RuleMatch(**conditions))
    except ValidationError as e:
        raise OpError("give at least one condition") from e


# --- add ------------------------------------------------------------------------------


class AddRuleArgs(MatchArgs):
    category: str = Field(description="category key or name the rule assigns")
    id: str | None = Field(None, description="rule id (default: made from the first pattern)")
    position: Literal["first", "last"] = Field(
        "last", description="first: wins over existing rules; last: only for what they miss"
    )
    apply: bool = Field(False, description="run the categorization again afterwards")


def run_add(ctx: OpContext, a: AddRuleArgs) -> Changed:
    from hiro_budget.services.config_edit import slugify, unique

    conditions = _conditions(a)
    with ctx.write("add_rule", a):
        with ctx.session() as s:
            category = resolve.category(s, a.category).key
        rules = list(ctx.settings.rules.rules)
        source = next((str(v) for v in conditions.values() if isinstance(v, str)), "rule")
        rule_id = a.id or unique(
            slugify(re.sub(r"\(\?i\)", "", source))[:40], {r.id for r in rules}
        )
        if any(r.id == rule_id for r in rules):
            raise OpError(f"a rule with the id '{rule_id}' exists")
        rule = _rule(rule_id, category, conditions)
        _save(ctx, [rule, *rules] if a.position == "first" else [*rules, rule])
        details = [_apply(ctx)] if a.apply else []
    return Changed(
        message=f"rule {rule_id} added ({a.position})",
        details=details,
        warnings=_warnings(ctx, rule),
    )


# --- update ---------------------------------------------------------------------------


class UpdateRuleArgs(MatchArgs):
    id: str = Field(description="id of the rule (see list_rules)", json_schema_extra=positional())
    category: str | None = Field(None, description="new category key or name")
    apply: bool = Field(False, description="run the categorization again afterwards")


def run_update(ctx: OpContext, a: UpdateRuleArgs) -> Changed:
    changes = _conditions(a)
    with ctx.write("update_rule", a):
        rules = list(ctx.settings.rules.rules)
        index = next((i for i, r in enumerate(rules) if r.id == a.id), None)
        if index is None:
            raise OpError(f"unknown rule '{a.id}'")
        old = rules[index]
        match = old.match.model_dump(exclude_none=True) | changes
        category = old.category
        if a.category:
            with ctx.session() as s:
                category = resolve.category(s, a.category).key
        if not changes and category == old.category:
            raise OpError("nothing to change; give a condition or --category")
        rules[index] = _rule(old.id, category, match)
        _save(ctx, rules)
        details = [_apply(ctx)] if a.apply else []
    return Changed(
        message=f"rule {a.id} changed", details=details, warnings=_warnings(ctx, rules[index])
    )


# --- delete ---------------------------------------------------------------------------


class DeleteRuleArgs(Args):
    id: str = Field(description="id of the rule (see list_rules)", json_schema_extra=positional())
    apply: bool = Field(False, description="run the categorization again afterwards")


def run_delete(ctx: OpContext, a: DeleteRuleArgs) -> Changed:
    with ctx.write("delete_rule", a):
        rules = list(ctx.settings.rules.rules)
        if not any(r.id == a.id for r in rules):
            raise OpError(f"unknown rule '{a.id}'")
        _save(ctx, [r for r in rules if r.id != a.id])
        details = [_apply(ctx)] if a.apply else []
    return Changed(message=f"rule {a.id} deleted", details=details)


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
        "add_rule",
        ("rules", "add"),
        "Add a categorization rule",
        "Add a rule: conditions (regular expressions; all must match) and the category it "
        "assigns. Try the conditions with test_rule first. The first matching rule wins; "
        "position says where the new rule goes. Manually categorized bookings are never "
        "changed; apply runs the categorization again.",
        AddRuleArgs,
        run_add,
    ),
    _op(
        "update_rule",
        ("rules", "edit"),
        "Change a rule's conditions or category",
        "Change given conditions of a rule (the others stay) or its category.",
        UpdateRuleArgs,
        run_update,
    ),
    _op(
        "delete_rule",
        ("rules", "delete"),
        "Delete a rule",
        "Delete a rule by id. Bookings it categorized keep their category until the "
        "categorization runs again.",
        DeleteRuleArgs,
        run_delete,
    ),
]
