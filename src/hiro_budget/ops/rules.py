"""Operations: list rules, test a rule definition without saving it."""

from __future__ import annotations

from pydantic import Field

from hiro_budget.ops import Args, OpContext, Operation, OpError, Result
from hiro_budget.ops.bookings import ListBookingsResult, booking_info, render_list
from hiro_budget.ops.text import Column, table


class ListRulesArgs(Args):
    pass


class RuleInfo(Result):
    id: str
    category: str
    match: dict[str, str | float] = Field(
        description="conditions (text values are regular expressions); all must match"
    )
    matching: int = Field(description="bookings the rule matches")
    effective: int = Field(description="of these, the bookings it categorizes (first rule wins)")
    uncategorized: int = Field(description="of the matching ones, currently without a category")


class ListRulesResult(Result):
    rules: list[RuleInfo]


def _match_dict(rule) -> dict[str, str | float]:
    return rule.match.model_dump(exclude_none=True)


def run_list(ctx: OpContext, a: ListRulesArgs) -> ListRulesResult:
    from hiro_budget.services.config_edit import rule_hits, rule_samples

    rules = ctx.settings.rules.rules
    with ctx.session() as s:
        hits = rule_hits(rule_samples(s), list(rules))
    return ListRulesResult(
        rules=[
            RuleInfo(
                id=r.id,
                category=r.category,
                match=_match_dict(r),
                matching=h.matching,
                effective=h.effective,
                uncategorized=h.uncategorized,
            )
            for r, h in zip(rules, hits, strict=True)
        ]
    )


def _match_text(match: dict[str, str | float]) -> str:
    return ", ".join(f"{k}={v}" for k, v in match.items())


def render_rules(r: ListRulesResult, width: int | None = None) -> str:
    cols = [
        Column("Id"),
        Column("Category", shrink=True),
        Column("Hits", "r"),
        Column("Used", "r"),
        Column("Uncat.", "r"),
        Column("Match", shrink=True),
    ]
    rows = [
        [
            x.id,
            x.category,
            str(x.matching),
            str(x.effective),
            str(x.uncategorized),
            _match_text(x.match),
        ]
        for x in r.rules
    ]
    return table(cols, rows, width)


COUNTERPARTY_HELP = "regex on the counterparty (the payee; also set for manual bookings)"
MERCHANT_HELP = "regex on the merchant (card payments; empty for manual bookings: use counterparty)"


def no_match_hint(conditions: dict) -> str:
    """Why a rule may match nothing, for the result of test_rule / add_rule / update_rule (#90)."""
    hint = "No booking matches these conditions."
    if "merchant" in conditions:
        hint += (
            " 'merchant' is only set for card payments, not for manual bookings: "
            "use 'counterparty' for the payee."
        )
    return hint


class RuleTestArgs(Args):
    counterparty: str | None = Field(None, description=COUNTERPARTY_HELP)
    merchant: str | None = Field(None, description=MERCHANT_HELP)
    purpose: str | None = Field(None, description="regex on the purpose")
    booking_type: str | None = Field(None, description="regex on the booking type")
    details: str | None = Field(None, description="regex on the details of a PayPal payment")
    account: str | None = Field(None, description="regex on the account name")
    amount_min: float | None = Field(None, description="smallest amount in euros (signed)")
    amount_max: float | None = Field(None, description="largest amount in euros (signed)")


class RuleTestResult(Result):
    matching: int
    uncategorized: int = Field(description="of the matching bookings, those without a category")
    samples: ListBookingsResult = Field(description="up to 10 of the newest matching bookings")
    hint: str | None = Field(None, description="set when nothing matches: why that may be")


def run_test(ctx: OpContext, a: RuleTestArgs) -> RuleTestResult:
    import re

    from pydantic import ValidationError

    from hiro_budget.config import RuleDef, RuleMatch
    from hiro_budget.services import transactions as txs
    from hiro_budget.services.config_edit import rule_hits, rule_samples

    conditions = a.model_dump(exclude_none=True)
    for name, value in conditions.items():
        if isinstance(value, str):
            try:
                re.compile(value)
            except re.error as e:
                raise OpError(f"{name}: not a valid regular expression: {e}") from e
    try:
        rule = RuleDef(id="test", category="test", match=RuleMatch(**conditions))
    except ValidationError as e:
        raise OpError("give at least one condition") from e
    with ctx.session() as s:
        (hits,) = rule_hits(rule_samples(s), [rule])
        newest = tuple(sorted(hits.ids)[-10:])
        rows = txs.list_transactions(s, txs.TxFilter(ids=newest)) if newest else []
    return RuleTestResult(
        matching=hits.matching,
        uncategorized=hits.uncategorized,
        samples=ListBookingsResult(
            bookings=[booking_info(x) for x in rows], total=hits.matching, offset=0
        ),
        hint=no_match_hint(conditions) if hits.matching == 0 else None,
    )


def render_test(r: RuleTestResult, width: int | None = None) -> str:
    head = f"The rule matches {r.matching} bookings ({r.uncategorized} uncategorized)."
    if not r.samples.bookings:
        return f"{head}\n{r.hint}" if r.hint else head
    return f"{head}\nNewest matches:\n{render_list(r.samples, width)}"


OPERATIONS = [
    Operation(
        name="list_rules",
        cli=("rules", "list"),
        summary="List categorization rules with their hit counts",
        description=(
            "The rules of rules.yaml in order (the first matching rule categorizes a "
            "booking). 'matching' counts the bookings a rule matches, 'effective' those it "
            "categorizes because no earlier rule took them."
        ),
        args=ListRulesArgs,
        result=ListRulesResult,
        run=run_list,
        render=render_rules,
        read_only=True,
    ),
    Operation(
        name="test_rule",
        cli=("rules", "test"),
        summary="Count the bookings a rule definition would match, without saving it",
        description=(
            "Try a rule before adding it: give the conditions (text values are regular "
            "expressions, all given conditions must match) and get the number of matching "
            "bookings and up to 10 newest examples. Nothing is saved."
        ),
        args=RuleTestArgs,
        result=RuleTestResult,
        run=run_test,
        render=render_test,
        read_only=True,
    ),
]
