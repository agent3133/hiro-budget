from datetime import date

import pytest
from sqlalchemy import select

from hiro_budget.config import RuleDef
from hiro_budget.db.models import Account, Category
from hiro_budget.services import config_edit, import_service, payee_review
from hiro_budget.services import transactions as txs
from hiro_budget.services.categorization.rules import CompiledRule


@pytest.fixture
def imported(app, statement_pdf):
    with app.session() as s:
        import_service.import_file(s, app.settings, statement_pdf)
    return app


def cat_id(s, key):
    return s.scalar(select(Category.id).where(Category.key == key))


def test_groups_by_payee_largest_first(imported) -> None:
    with imported.session() as s:
        giro = s.scalar(select(Account.id).where(Account.name == "ING Girokonto"))
        # a second uncategorized booking of a payee: its group comes first
        txs.create_manual(
            s,
            imported.settings,
            txs.ManualEntry(giro, date(2026, 3, 20), -5_000, "handwerker gmbh"),
        )
        groups = payee_review.uncategorized_groups(s)
        assert [g.source.casefold() for g in groups][0] == "handwerker gmbh"
        top = groups[0]
        assert top.count == 2 and top.total_cents == -17_000  # same payee, any case
        assert top.field == "counterparty"
        assert sum(g.count for g in groups) == txs.count_transactions(
            s, txs.TxFilter(category=txs.UNCATEGORIZED)
        )


def test_suggestion_from_earlier_bookings(imported) -> None:
    with imported.session() as s:
        giro = s.scalar(select(Account.id).where(Account.name == "ING Girokonto"))
        handwerker = cat_id(s, "wohnen.instandhaltung")
        txs.create_manual(
            s,
            imported.settings,
            txs.ManualEntry(
                giro, date(2026, 3, 21), -800, "Handwerker GmbH", category_id=handwerker
            ),
        )
        groups = {g.source: g for g in payee_review.uncategorized_groups(s)}
        assert handwerker is not None
        assert groups["Handwerker GmbH"].suggestion == handwerker
        assert groups["Musterfrau"].suggestion is None


def test_assign_group_and_rule(imported) -> None:
    with imported.session() as s:
        group = next(
            g for g in payee_review.uncategorized_groups(s) if g.source == "Handwerker GmbH"
        )
        payee_review.assign(
            s, [r.id for r in group.rows], category_id=cat_id(s, "wohnen.instandhaltung")
        )
        sources = [g.source for g in payee_review.uncategorized_groups(s)]
        assert "Handwerker GmbH" not in sources
        assert txs.get_row(s, group.rows[0].id).splits[0].category_key == "wohnen.instandhaltung"

    rule = payee_review.payee_rule(group, "wohnen.instandhaltung", "handwerker_gmbh")
    assert rule.match.counterparty == r"(?i)Handwerker\ GmbH"
    compiled = CompiledRule.compile(rule)
    with imported.session() as s:
        total, _ = config_edit.rule_match_count(s, rule)
    assert total == 1 and compiled is not None
    # saved in front of the other rules (the first match wins)
    rules = imported.settings.rules.rules
    config_edit.save_rules(imported.settings, [rule, *rules])
    assert isinstance(imported.settings.rules.rules[0], RuleDef)
    assert imported.settings.rules.rules[0].id == "handwerker_gmbh"


def test_count_transactions_follows_the_filter(imported) -> None:
    with imported.session() as s:
        everything = txs.count_transactions(s, txs.TxFilter())
        assert everything == len(txs.list_transactions(s))
        march2 = txs.TxFilter(date_from=date(2026, 3, 2), date_to=date(2026, 3, 2))
        assert txs.count_transactions(s, march2) == 2
