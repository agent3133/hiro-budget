"""YAML rule matching."""

from __future__ import annotations

import re
from dataclasses import dataclass

from hiro_budget.config import RuleDef
from hiro_budget.db.models import Transaction
from hiro_budget.money import euros_to_cents

TEXT_FIELDS = ("counterparty", "merchant", "purpose", "booking_type", "details")


@dataclass
class CompiledRule:
    id: str
    category: str
    patterns: dict[str, re.Pattern[str]]
    amount_min: int | None
    amount_max: int | None

    @classmethod
    def compile(cls, rule: RuleDef) -> CompiledRule:
        m = rule.match
        patterns = {
            name: re.compile(value)
            for name in (*TEXT_FIELDS, "account")
            if (value := getattr(m, name)) is not None
        }
        return cls(
            id=rule.id,
            category=rule.category,
            patterns=patterns,
            amount_min=euros_to_cents(m.amount_min) if m.amount_min is not None else None,
            amount_max=euros_to_cents(m.amount_max) if m.amount_max is not None else None,
        )

    def matches(self, tx: Transaction) -> bool:
        if self.amount_min is not None and tx.amount_cents < self.amount_min:
            return False
        if self.amount_max is not None and tx.amount_cents > self.amount_max:
            return False
        for name, rx in self.patterns.items():
            if name == "account":
                value = tx.account.name if tx.account else ""
            else:
                value = getattr(tx, name) or ""
            if not rx.search(value):
                return False
        return True


def compile_rules(rules: list[RuleDef]) -> list[CompiledRule]:
    return [CompiledRule.compile(r) for r in rules]


def first_match(rules: list[CompiledRule], tx: Transaction) -> CompiledRule | None:
    return next((r for r in rules if r.matches(tx)), None)
