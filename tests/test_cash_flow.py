"""Cash-flow report (Sankey): service and page."""

from collections import defaultdict
from datetime import date

import pytest
from PySide6.QtWidgets import QMessageBox

from hiro_budget.services import cash_flow, reports
from hiro_budget.services.cash_flow import (
    EXPENSE_CATEGORY,
    EXPENSE_GROUP,
    INCOME_CATEGORY,
    INCOME_GROUP,
)
from tests.test_report_options import REPAIRS, book, configure

APRIL = (date(2026, 4, 1), date(2026, 4, 30))


@pytest.fixture(autouse=True)
def no_message_boxes(monkeypatch):
    for name in ("information", "warning", "critical"):
        monkeypatch.setattr(QMessageBox, name, lambda *a, **k: QMessageBox.StandardButton.Ok)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.No)


@pytest.fixture
def flow_app(config_dir):
    from hiro_budget.app import App

    configure(config_dir)  # REPAIRS is a one-off category
    app = App.from_config_dir(config_dir)
    book(app, date(2026, 4, 25), 3000_00, "einkommen.gehalt", "Arbeitgeber")
    book(app, date(2026, 4, 10), 40_00, "einkommen.kapitalertraege", "Depot")
    book(app, date(2026, 4, 3), -500_00, "lebensmittel.supermarkt", "Rewe")
    book(app, date(2026, 4, 4), -10_00, "lebensmittel.baeckerei", "Bäcker")  # < 1 %
    book(app, date(2026, 4, 5), -20_00, "lebensmittel.drogerie", "dm")  # < 1 %
    book(app, date(2026, 4, 6), -100_00, "wohnen.energie", "Stadtwerke")
    book(app, date(2026, 4, 7), 50_00, "wohnen.nebenkosten", "Hausverwaltung")  # refund only
    book(app, date(2026, 4, 15), -200_00, REPAIRS, "Dachdecker")
    return app


def flow(app, one_offs: bool = True) -> cash_flow.CashFlow:
    with app.session() as s:
        return cash_flow.cash_flow(s, *APRIL, one_offs=one_offs)


def by_name(data: cash_flow.CashFlow, column: int) -> dict[str, int]:
    return {n.name or n.kind: n.cents for n in data.nodes if n.column == column}


def test_columns_and_sums(flow_app) -> None:
    data = flow(flow_app)
    assert by_name(data, INCOME_CATEGORY) == {"Gehalt": 3000_00, "Kapitalerträge": 40_00}
    assert by_name(data, INCOME_GROUP) == {"Einkommen": 3040_00}
    assert by_name(data, EXPENSE_GROUP) == {
        "Lebensmittel": 530_00,
        "Wohnen": 300_00,
        "surplus": 2210_00,
    }
    # bakery and drugstore are below 1 % of 3.040 €: one "other" node of their group
    assert by_name(data, EXPENSE_CATEGORY) == {
        "Supermarkt": 500_00,
        "Lebensmittel": 30_00,
        "Instandhaltung & Baumarkt": 200_00,
        "Energie": 100_00,
    }
    other = next(n for n in data.nodes if n.kind == "other")
    assert len(other.category_ids) == 2
    # the refund-only category cannot be a flow
    assert [(o.name, o.cents) for o in data.left_out] == [("Wohnen: Nebenkosten/Hausgeld", -50_00)]


def test_flows_are_conserved(flow_app) -> None:
    data = flow(flow_app)
    into, out = defaultdict(int), defaultdict(int)
    for link in data.links:
        out[link.source] += link.cents
        into[link.target] += link.cents
        assert data.nodes[link.target].column == data.nodes[link.source].column + 1
    for i, n in enumerate(data.nodes):  # what flows in and out of a node is its sum
        if n.column != INCOME_CATEGORY and n.kind not in ("deficit", "uncategorized"):
            assert into[i] == n.cents
        if n.column != EXPENSE_CATEGORY and n.kind != "surplus":
            assert out[i] == n.cents
    sides = defaultdict(int)
    for n in data.nodes:
        sides[n.column] += n.cents
    assert sides[INCOME_GROUP] == sides[EXPENSE_GROUP] == data.total_cents


def test_matches_the_report_totals(flow_app) -> None:
    for one_offs in (True, False):
        data = flow(flow_app, one_offs)
        with flow_app.session() as s:
            (month,) = reports.monthly_totals(s, *APRIL, one_offs=one_offs)
        left_out = sum(o.cents for o in data.left_out if not o.income)
        assert data.income_cents == month.income_cents
        assert data.expense_cents + left_out == month.expense_cents
        assert data.one_off_expense_cents == month.one_off_expense_cents == 200_00


def test_one_offs_can_be_left_out(flow_app) -> None:
    data = flow(flow_app, one_offs=False)
    assert "Instandhaltung & Baumarkt" not in by_name(data, EXPENSE_CATEGORY)
    assert data.expense_cents == 630_00


def test_deficit_comes_from_reserves(flow_app) -> None:
    book(flow_app, date(2026, 4, 20), -5000_00, "wohnen.instandhaltung", "Dachdecker")
    data = flow(flow_app)
    deficit = next(n for n in data.nodes if n.kind == "deficit")
    assert deficit.column == INCOME_GROUP and deficit.income
    assert deficit.cents == data.expense_cents - data.income_cents
    assert not any(n.kind == "surplus" for n in data.nodes)


def test_empty_period(flow_app) -> None:
    with flow_app.session() as s:
        data = cash_flow.cash_flow(s, date(2020, 1, 1), date(2020, 1, 31))
    assert data.nodes == [] and data.links == [] and data.total_cents == 0


def test_middle_links_split_in_proportion() -> None:
    nodes = [
        cash_flow.Node(INCOME_GROUP, "group", "A", 300, True),
        cash_flow.Node(INCOME_GROUP, "group", "B", 101, True),
        cash_flow.Node(EXPENSE_GROUP, "group", "X", 200, False),
        cash_flow.Node(EXPENSE_GROUP, "group", "Y", 201, False),
    ]
    links = cash_flow._split_middle(nodes, [0, 1], [2, 3])
    for source in (0, 1):
        assert sum(k.cents for k in links if k.source == source) == nodes[source].cents


# --- GUI -------------------------------------------------------------------------------


def test_page_draws_and_opens_breakdowns(qtbot, flow_app) -> None:
    from hiro_budget.gui.report_pages import CashFlowReport

    page = CashFlowReport(flow_app)
    qtbot.addWidget(page)
    page.resize(1200, 700)
    page.show()
    page.refresh(*APRIL, None)
    chart = page.chart
    assert len(chart.nodes) == len(page.data.nodes) and len(chart.links) == len(page.data.links)
    assert "Nebenkosten" in page.note.text()  # left out, named
    chart.grab()  # paints (lays out) without errors
    assert all(b.rect.height() > 0 for b in chart._boxes)
    # labels never overlap: each slot is at least a text line high
    for col in range(4):
        boxes = [chart._boxes[i].slot for i, n in enumerate(chart.nodes) if n.column == col]
        slots = sorted((s.top(), s.bottom()) for s in boxes)
        assert all(a[1] <= b[0] for a, b in zip(slots, slots[1:], strict=False))

    nodes = page.data.nodes
    supermarket = next(i for i, n in enumerate(nodes) if n.name == "Supermarkt")
    chart.node_activated.emit(supermarket)
    assert page.breakdown_dialog.breakdown.total == 500_00
    group = next(i for i, n in enumerate(nodes) if n.kind == "group" and not n.income)
    chart.node_activated.emit(group)
    assert page.breakdown_dialog.breakdown.total == nodes[group].cents
    surplus = next(i for i, n in enumerate(nodes) if n.kind == "surplus")
    chart.node_activated.emit(surplus)
    # saved = all income − all expenses (the refund included)
    assert page.breakdown_dialog.breakdown.total == nodes[surplus].cents + 50_00


def test_page_empty_period(qtbot, flow_app) -> None:
    from hiro_budget.gui.report_pages import CashFlowReport

    page = CashFlowReport(flow_app)
    qtbot.addWidget(page)
    page.refresh(date(2020, 1, 1), date(2020, 1, 31), None)
    assert page.chart.nodes == [] and page.note.text() == ""
    page.chart.grab()


def test_expense_groups_in_the_budget_colours(qtbot, flow_app, monkeypatch) -> None:
    from hiro_budget.gui import theme
    from hiro_budget.gui.budget_view import CHIP_ROLE, GROUP_ROLE, BudgetView
    from hiro_budget.gui.report_pages import CashFlowReport

    monkeypatch.setattr(theme, "_active", "rose_pine")  # the theme with group colours
    page = CashFlowReport(flow_app)
    qtbot.addWidget(page)
    page.refresh(*APRIL, None)
    sankey = {
        n.name: page.chart.nodes[i].color.name()
        for i, n in enumerate(page.data.nodes)
        if n.kind == "group" and not n.income
    }
    for i, n in enumerate(page.data.nodes):  # categories in the colour of their group
        if n.kind == "category" and not n.income:
            group = next(
                g for g in page.data.nodes if g.kind == "group" and g.group_id == n.group_id
            )
            assert page.chart.nodes[i].color.name() == sankey[group.name]

    view = BudgetView(flow_app)
    qtbot.addWidget(view)
    view.go("2026-04")
    tree = view.tree
    chips = {
        tree.topLevelItem(r).text(0): tree.topLevelItem(r).data(0, CHIP_ROLE)
        for r in range(tree.topLevelItemCount())
        if tree.topLevelItem(r).data(0, GROUP_ROLE)
    }
    assert sankey and all(chips[name].lower() == color for name, color in sankey.items())
