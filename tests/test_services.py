from datetime import date

import pytest
from sqlalchemy import select

from hiro_budget.config import ConfigError, RuleDef, load_settings
from hiro_budget.db.models import Account, Category, SplitSource
from hiro_budget.services import config_edit, import_service, reports
from hiro_budget.services import transactions as txs
from tests.fixtures import ing_statement as fx


@pytest.fixture
def imported(app, statement_pdf):
    with app.session() as s:
        import_service.import_file(s, app.settings, statement_pdf)
    return app


def cat_id(s, key):
    return s.scalar(select(Category.id).where(Category.key == key))


def test_list_and_filter(imported) -> None:
    with imported.session() as s:
        rows = txs.list_transactions(s)
        assert len(rows) == fx.BOOKING_COUNT + 1  # + loan counter-booking
        assert rows[0].booking_date >= rows[-1].booking_date  # newest first
        assert len(txs.list_transactions(s, txs.TxFilter(text="vinted"))) == 2
        # mortgage booking (principal) and its counter-booking on the loan account
        assert len(txs.list_transactions(s, txs.TxFilter(category=txs.TRANSFERS))) == 2
        groceries = txs.list_transactions(
            s, txs.TxFilter(category=cat_id(s, "lebensmittel.supermarkt"))
        )
        assert [r.payee for r in groceries] == ["REWE MUSTERSTADT"]
        march2 = txs.list_transactions(
            s, txs.TxFilter(date_from=date(2026, 3, 2), date_to=date(2026, 3, 2))
        )
        assert len(march2) == 2


def test_manual_edits(imported) -> None:
    with imported.session() as s:
        row = txs.list_transactions(s, txs.TxFilter(text="Handwerker"))[0]
        assert row.is_uncategorized
        txs.set_category(s, row.id, cat_id(s, "wohnen.instandhaltung"))
        row = txs.get_row(s, row.id)
        assert row.is_manual and row.splits[0].category_name == "Instandhaltung & Baumarkt"

        # split -120,00 into two parts
        with pytest.raises(txs.SplitError):
            txs.set_splits(s, row.id, [txs.SplitInput(-100_00, cat_id(s, "wohnen.energie"))])
        txs.set_splits(
            s,
            row.id,
            [
                txs.SplitInput(-100_00, cat_id(s, "wohnen.instandhaltung")),
                txs.SplitInput(-20_00, cat_id(s, "wohnen.energie"), memo="Material"),
            ],
        )
        assert txs.get_row(s, row.id).is_split

        savings = s.scalar(select(Account.id).where(Account.name == "ING Extra-Konto"))
        txs.mark_transfer(s, row.id, savings)
        assert txs.get_row(s, row.id).splits[0].transfer_account_name == "ING Extra-Konto"

        txs.reset_to_automatic(s, imported.settings, [row.id])
        assert txs.get_row(s, row.id).is_uncategorized


def test_reports(imported) -> None:
    with imported.session() as s:
        months = reports.monthly_totals(s)
        assert len(months) == 1
        m = months[0]
        assert m.income_cents == 4000_00
        # all outflows except the mortgage principal (transfer)
        assert m.expense_cents == 250_00 + 23_45 + 2 * 9_09 + 32_50 + 120_00 + 600_00
        cats = {c.key: c.expense_cents for c in reports.category_totals(s)}
        assert cats["wohnen.baufinanzierung"] == 600_00
        assert cats["sonstiges.online"] == 2 * 9_09 + 32_50  # vinted + paypal ebay
        assert cats[reports.UNCATEGORIZED_KEY] == 250_00 + 120_00
        trend = reports.category_trend(s, cat_id(s, "einkommen.gehalt"))
        assert trend == [("2026-03", 4000_00)]


def test_months_between() -> None:
    assert reports.months_between("2025-11", "2026-02") == [
        "2025-11",
        "2025-12",
        "2026-01",
        "2026-02",
    ]


def test_rule_match_count(imported) -> None:
    with imported.session() as s:
        rule = RuleDef(
            id="hw", category="wohnen.instandhaltung", match={"counterparty": "Handwerk"}
        )
        assert config_edit.rule_match_count(s, rule) == (1, 1)


def test_save_rules_roundtrip(app, config_dir) -> None:
    rules = list(app.settings.rules.rules)
    rules.insert(
        0, RuleDef(id="hw", category="wohnen.instandhaltung", match={"counterparty": "Handwerk"})
    )
    config_edit.save_rules(app.settings, rules)
    text = (config_dir / "rules.yaml").read_text(encoding="utf-8")
    assert text.startswith("# Categorization rules")  # header comment kept
    assert load_settings(config_dir).rules.rules[0].id == "hw"

    bad = [RuleDef(id="x", category="nope.nope", match={"merchant": "x"})]
    with pytest.raises(ConfigError):
        config_edit.save_rules(app.settings, bad)


def test_save_categories_refuses_removing_used_category(app) -> None:
    cfg = app.settings.categories.model_copy(deep=True)
    cfg.groups = [g for g in cfg.groups if g.key != "lebensmittel"]
    with pytest.raises(ConfigError, match="supermarkt"):
        config_edit.save_categories(app.settings, cfg)


def test_save_categories_rename(app, config_dir) -> None:
    cfg = app.settings.categories.model_copy(deep=True)
    cfg.groups[0].categories[0].name = "Lohn"
    config_edit.save_categories(app.settings, cfg)
    reloaded = load_settings(config_dir)
    assert reloaded.categories.groups[0].categories[0].name == "Lohn"
    assert reloaded.categories.groups[0].income


def test_categorization_preview_does_not_persist(app, statement_pdf) -> None:
    with app.session() as s:
        p = import_service.preview(s, statement_pdf)
    with app.session() as s:
        labels = import_service.categorization_preview(s, app.settings, p)
    assert labels[1] == "Supermarkt"
    assert labels[-1] == "Baufinanzierung + ⇄ ING Baufinanzierung"
    with app.session() as s:
        assert txs.list_transactions(s) == []
        assert import_service.preview(s, statement_pdf).can_commit


def test_accept_suggestion(imported) -> None:
    with imported.session() as s:
        row = txs.list_transactions(s, txs.TxFilter(text="REWE"))[0]
        txs.accept_suggestion(s, row.id)
        assert txs.get_row(s, row.id).splits[0].source == SplitSource.MANUAL
