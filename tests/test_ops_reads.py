"""The read operations (#83): the numbers equal the services' numbers."""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import select

from hiro_budget import ops
from hiro_budget.db.models import Account, Category
from hiro_budget.ops import OpContext, OpError
from hiro_budget.services import budget, reports
from hiro_budget.services import transactions as txs
from hiro_budget.services.transactions import ManualEntry


@pytest.fixture
def ctx(app) -> OpContext:
    return OpContext(app, source="test")


def _account(s, name: str) -> int:
    return s.scalar(select(Account.id).where(Account.name == name))


def _category(s, key: str) -> int:
    return s.scalar(select(Category.id).where(Category.key == key))


@pytest.fixture
def data(app, ctx) -> dict[str, int]:
    """Four bookings on the cash account: two categorized, one not, one income."""
    with app.session() as s:
        cash = _account(s, "Bargeld")
        food = _category(s, "lebensmittel.supermarkt")
        ids = {}
        for name, day, cents, cat in (
            ("rewe", date(2026, 3, 5), -2550, food),
            ("aldi", date(2026, 3, 9), -1999, food),
            ("mystery", date(2026, 3, 12), -700, None),
            ("pay", date(2026, 3, 15), 50000, _category(s, "einkommen.gehalt")),
        ):
            entry = ManualEntry(cash, day, cents, name, category_id=cat)
            ids[name] = txs.create_manual(s, app.settings, entry)
    return ids


def call(ctx, name, **args):
    return ops.call(ctx, name, args)


def test_overview_matches_service(ctx, data):
    r = call(ctx, "get_overview")
    from hiro_budget.services.overview import overview

    with ctx.session() as s:
        o = overview(s, ctx.settings)
    assert r.month == o.month
    assert r.ready_to_assign == f"{o.ready_to_assign / 100:.2f}"
    assert r.uncategorized_bookings == o.uncategorized
    assert "Ready to Assign" in ops.find("get_overview").render(r, None)


def test_overview_separates_overdue_from_upcoming(ctx, data, monkeypatch):
    from types import SimpleNamespace

    from hiro_budget.ops import overview as ops_overview
    from hiro_budget.services import overview as service

    def due(day: date, payee: str, cents: int):
        return SimpleNamespace(next_due=day, payee=payee, amount_cents=cents)

    items = [
        due(date(2026, 10, 30), "Seidel", 124000),
        due(date(2026, 11, 4), "Octopus", 10400),  # today
        due(date(2026, 11, 14), "ENTEGA", 15800),
        due(date(2026, 12, 20), "Later", 100),  # beyond the 30 days: not shown
    ]
    monkeypatch.setattr(ops_overview, "_today", lambda: date(2026, 11, 4))
    monkeypatch.setattr(
        service, "recurring_costs", lambda session, today: SimpleNamespace(items=items)
    )
    r = call(ctx, "get_overview")
    assert r.overdue == ["2026-10-30 Seidel 1.240,00"]
    assert r.upcoming == ["2026-11-04 Octopus 104,00", "2026-11-14 ENTEGA 158,00"]
    text = ops.find("get_overview").render(r, None)
    assert text.index("Overdue:") < text.index("Seidel") < text.index("Upcoming:")
    assert text.index("Upcoming:") < text.index("Octopus")


def test_accounts_list(ctx, data):
    r = call(ctx, "list_accounts")
    cash = next(a for a in r.accounts if a.name == "Bargeld")
    assert cash.bookings == 4
    # opening balance 100,00 + 500,00 - 25,50 - 19,99 - 7,00
    assert cash.balance == "547.51"
    text = ops.find("list_accounts").render(r, None)
    assert "Account" in text and "547,51" in text


def test_budget_show_matches_service(ctx, data):
    r = call(ctx, "get_budget", month="2026-03")
    with ctx.session() as s:
        b = budget.compute_month(s, ctx.settings, "2026-03")
    assert r.ready_to_assign == f"{b.ready_to_assign / 100:.2f}"
    food = next(c for g in r.groups for c in g.lines if c.key == "lebensmittel.supermarkt")
    assert food.activity == "-45.49"
    text = ops.find("get_budget").render(r, None)
    assert "Ready to Assign" in text and "Assigned" in text


def test_budget_show_rejects_bad_month(ctx):
    with pytest.raises(OpError, match="YYYY-MM"):
        call(ctx, "get_budget", month="march")


def test_list_bookings_filters_and_paging(ctx, data):
    all_ = call(ctx, "list_bookings", account="Bargeld")
    assert all_.total == 4 and len(all_.bookings) == 4
    assert [b.id for b in all_.bookings] == sorted(data.values(), reverse=True)  # newest first

    page = call(ctx, "list_bookings", account="Bargeld", limit=2, offset=1)
    assert page.total == 4
    assert [b.id for b in page.bookings] == [b.id for b in all_.bookings[1:3]]

    uncat = call(ctx, "list_bookings", category="uncategorized")
    assert [b.id for b in uncat.bookings] == [data["mystery"]]

    food = call(ctx, "list_bookings", category="lebensmittel.supermarkt")
    assert food.total == 2

    by_name = call(ctx, "list_bookings", category="Supermarkt")
    assert by_name.total == 2

    by_amount = call(ctx, "list_bookings", amount="-25,50")
    assert [b.id for b in by_amount.bookings] == [data["rewe"]]

    by_text = call(ctx, "list_bookings", text="aldi")
    assert [b.id for b in by_text.bookings] == [data["aldi"]]

    march = call(ctx, "list_bookings", date_from="2026-03-10", date_to="2026-03-14")
    assert [b.id for b in march.bookings] == [data["mystery"]]


def test_list_bookings_matches_service(ctx, data):
    r = call(ctx, "list_bookings", account="Bargeld")
    with ctx.session() as s:
        rows = txs.list_transactions(s, txs.TxFilter(account_id=_account(s, "Bargeld")))
    assert [(b.id, b.amount) for b in r.bookings] == [
        (x.id, f"{x.amount_cents / 100:.2f}") for x in rows
    ]


def test_unknown_category_suggests(ctx, data):
    with pytest.raises(OpError, match="Did you mean.*lebensmittel.supermarkt"):
        call(ctx, "list_bookings", category="supermarket")


def test_unknown_account(ctx, data):
    with pytest.raises(OpError, match="unknown account"):
        call(ctx, "list_bookings", account="Nowhere")


def test_get_booking(ctx, data):
    r = call(ctx, "get_booking", id=data["rewe"])
    assert r.amount == "-25.50" and r.manual
    assert r.splits[0].category == "lebensmittel.supermarkt"
    assert "Splits:" in ops.find("get_booking").render(r, None)
    with pytest.raises(OpError, match="no booking"):
        call(ctx, "get_booking", id=999999)


def test_list_categories(ctx):
    r = call(ctx, "list_categories")
    keys = [c.key for g in r.groups for c in g.categories]
    assert keys == ctx.settings.categories.full_keys()


def test_list_rules_and_test_rule(ctx, data):
    r = call(ctx, "list_rules")
    assert [x.id for x in r.rules] == [x.id for x in ctx.settings.rules.rules]
    t = call(ctx, "test_rule", purpose="(?i)^no such purpose$")
    assert t.matching == 0
    t = call(ctx, "test_rule", counterparty="(?i)aldi")
    assert t.matching == 1 and t.samples.bookings[0].id == data["aldi"]
    with pytest.raises(OpError, match="regular expression"):
        call(ctx, "test_rule", purpose="(")
    with pytest.raises(OpError, match="at least one condition"):
        call(ctx, "test_rule")


def test_reports_match_services(ctx, data):
    d1, d2 = date(2026, 3, 1), date(2026, 3, 31)
    with ctx.session() as s:
        months = reports.monthly_totals(s, d1, d2)
        cats = reports.category_totals(s, d1, d2)
    m = call(ctx, "report_monthly", date_from=d1, date_to=d2)
    assert [(x.month, x.income, x.expenses) for x in m.months] == [
        (x.month, f"{x.income_cents / 100:.2f}", f"{x.expense_cents / 100:.2f}") for x in months
    ]
    c = call(ctx, "report_categories", date_from=d1, date_to=d2)
    assert [(x.key, x.expenses) for x in c.categories] == [
        (x.key, f"{x.expense_cents / 100:.2f}") for x in cats
    ]
    assert c.total == "52.49"  # 25,50 + 19,99 + 7,00 (uncategorized); the salary is income


def test_report_payees_years_budget_networth_explain(ctx, data):
    p = call(ctx, "report_payees", limit=2)
    assert len(p.payees) == 2 and p.total_payees >= 3
    y = call(ctx, "report_years", first_year=2025, second_year=2026)
    assert y.second_year == 2026
    b = call(ctx, "report_budget_vs_actual", first_month="2026-03")
    assert b.first_month == b.last_month == "2026-03"
    w = call(ctx, "report_net_worth", months=2)
    assert len(w.months) <= 2
    e = call(ctx, "explain_value", what="expenses", date_from="2026-03-01", date_to="2026-03-31")
    assert (
        e.total
        == call(ctx, "report_monthly", date_from="2026-03-01", date_to="2026-03-31").expenses
    )
    ids = {x.id for p in e.parts for x in p.bookings}
    assert data["rewe"] in ids and data["pay"] not in ids
    with pytest.raises(OpError, match="needs --months"):
        call(ctx, "explain_value", what="budget")


def test_list_imports_empty(ctx, data):
    r = call(ctx, "list_imports")
    assert r.imports == []  # manual bookings are no imports
    assert ops.find("list_imports").render(r, None) == "(none)"


def test_invalid_arguments_are_op_errors(ctx):
    with pytest.raises(OpError, match="invalid arguments"):
        call(ctx, "list_bookings", limit=0)
    with pytest.raises(OpError, match="invalid arguments"):
        call(ctx, "list_bookings", bogus=1)
