"""Reporting month (``report_shift_days``) and one-off categories in the reports."""

from datetime import date

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QMessageBox
from sqlalchemy import select

from hiro_budget.config import load_settings
from hiro_budget.db.models import Account, Category
from hiro_budget.services import budget, budget_report, config_edit, reports, savings
from hiro_budget.services import transactions as txs
from hiro_budget.services.transactions import ManualEntry

MORTGAGE = "wohnen.baufinanzierung"  # report_shift_days: 5
REPAIRS = "wohnen.instandhaltung"  # one-off expense
REFUND = "einkommen.erstattungen"  # one-off income


@pytest.fixture(autouse=True)
def no_message_boxes(monkeypatch):
    for name in ("information", "warning", "critical"):
        monkeypatch.setattr(QMessageBox, name, lambda *a, **k: QMessageBox.StandardButton.Ok)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.No)


def configure(config_dir, group_budget: bool = False) -> None:
    path = config_dir / "categories.yaml"
    text = path.read_text(encoding="utf-8")
    for old, new in (
        (
            "{key: baufinanzierung, name: Baufinanzierung}",
            "{key: baufinanzierung, name: Baufinanzierung, report_shift_days: 5}",
        ),
        (
            "{key: instandhaltung, name: Instandhaltung & Baumarkt}",
            "{key: instandhaltung, name: Instandhaltung & Baumarkt, one_off: true}",
        ),
        (
            "{key: erstattungen, name: Erstattungen}",
            "{key: erstattungen, name: Erstattungen, one_off: true}",
        ),
    ):
        assert old in text
        text = text.replace(old, new)
    if group_budget:
        old = "  - key: wohnen\n    name: Wohnen\n"
        assert old in text
        text = text.replace(old, old + "    budget: group\n")
    path.write_text(text, encoding="utf-8")


def category_id(s, key: str) -> int:
    return s.scalar(select(Category.id).where(Category.key == key))


def book(app, day: date, cents: int, key: str, payee: str) -> None:
    with app.session() as s:
        cash = s.scalar(select(Account.id).where(Account.name == "Bargeld"))
        txs.create_manual(
            s, app.settings, ManualEntry(cash, day, cents, payee, category_id=category_id(s, key))
        )


def make_app(config_dir, group_budget: bool = False):
    from hiro_budget.app import App

    configure(config_dir, group_budget)
    app = App.from_config_dir(config_dir)
    book(app, date(2026, 4, 2), -1000_00, MORTGAGE, "Bank")  # due in March
    book(app, date(2026, 4, 6), -1000_00, MORTGAGE, "Bank")  # day 6: April
    book(app, date(2026, 4, 2), -50_00, "lebensmittel.supermarkt", "Rewe")  # not shifted
    book(app, date(2026, 4, 15), -5000_00, REPAIRS, "Dachdecker")
    book(app, date(2026, 4, 20), 300_00, REFUND, "Finanzamt")
    book(app, date(2026, 4, 25), 3000_00, "einkommen.gehalt", "Arbeitgeber")
    return app


@pytest.fixture
def options_app(config_dir):
    return make_app(config_dir)


# --- services ------------------------------------------------------------------------


def test_shifted_bookings_count_for_the_previous_month(options_app) -> None:
    with options_app.session() as s:
        months = {m.month: m for m in reports.monthly_totals(s)}
        march = reports.category_totals(s, date(2026, 3, 1), date(2026, 3, 31))
        april = reports.category_totals(s, date(2026, 4, 1), date(2026, 4, 30))
        trend = reports.category_trend(s, category_id(s, MORTGAGE))
        shifted = reports.shifted_categories(s)
    assert months["2026-03"].expense_cents == 1000_00
    assert months["2026-04"].expense_cents == 1000_00 + 50_00 + 5000_00
    # the period filter follows the reporting month
    assert {t.key: t.expense_cents for t in march} == {MORTGAGE: 1000_00}
    by_key = {t.key: t.expense_cents for t in april}
    assert by_key[MORTGAGE] == 1000_00 and by_key["lebensmittel.supermarkt"] == 50_00
    assert trend == [("2026-03", -1000_00), ("2026-04", -1000_00)]
    assert shifted == [("Baufinanzierung", 5)]


def test_one_offs_can_be_left_out(options_app) -> None:
    with options_app.session() as s:
        with_all = {m.month: m for m in reports.monthly_totals(s)}["2026-04"]
        regular = {m.month: m for m in reports.monthly_totals(s, one_offs=False)}["2026-04"]
        cats = {t.key: t for t in reports.category_totals(s)}
        regular_cats = {t.key for t in reports.category_totals(s, one_offs=False)}
        payees = {p.name for p in reports.top_payees(s)}
        regular_payees = {p.name for p in reports.top_payees(s, one_offs=False)}
    assert (with_all.income_cents, regular.income_cents) == (3300_00, 3000_00)
    assert (with_all.expense_cents, regular.expense_cents) == (6050_00, 1050_00)
    for m in (with_all, regular):  # what was left out is known either way
        assert (m.one_off_income_cents, m.one_off_expense_cents) == (300_00, 5000_00)
    assert cats[REPAIRS].one_off and not cats[MORTGAGE].one_off
    assert REPAIRS not in regular_cats and MORTGAGE in regular_cats
    assert "Dachdecker" in payees and "Dachdecker" not in regular_payees


def test_year_comparison_and_savings_rate_follow_the_switch(options_app) -> None:
    with options_app.session() as s:
        years = reports.year_comparison(s, 2025, 2026, one_offs=False)
        rates = savings.savings_rates(s, date(2026, 3, 1), date(2026, 4, 30), one_offs=False)
        all_rates = savings.savings_rates(s, date(2026, 3, 1), date(2026, 4, 30))
    assert REPAIRS not in {r.key for r in years.rows}
    assert years.income == (0, 3000_00)
    assert years.one_off_income == (0, 300_00) and years.one_off_expenses == (0, 5000_00)
    april = {m.month: m for m in rates.months}["2026-04"]
    assert april.rate == pytest.approx((3000_00 - 1050_00) / 3000_00)
    assert april.one_off_net_cents == 300_00 - 5000_00
    assert rates.one_off_expense_cents == all_rates.one_off_expense_cents == 5000_00
    assert {m.month: m for m in all_rates.months}["2026-04"].rate < 0


def test_budget_vs_actual_uses_the_reporting_month(options_app) -> None:
    settings = options_app.settings
    with options_app.session() as s:
        cash_april = budget.compute_month(s, settings, "2026-04")
        data = budget_report.budget_vs_actual(s, settings, "2026-03", "2026-04")
        regular = budget_report.budget_vs_actual(s, settings, "2026-03", "2026-04", one_offs=False)
    # the budget itself stays on booking dates
    assert cash_april.category(category_id_of(options_app, MORTGAGE)).activity == -2000_00
    assert [(m.month, m.spent) for m in data.months] == [
        ("2026-03", 1000_00),
        ("2026-04", 1000_00 + 50_00 + 5000_00),
    ]
    lines = {line.key: line for line in data.lines}
    assert lines[REPAIRS].one_off and lines[MORTGAGE].spent == 2000_00
    assert REPAIRS not in {line.key for line in regular.lines}
    assert regular.spent == data.spent - 5000_00
    assert data.one_off_spent == regular.one_off_spent == 5000_00


def category_id_of(app, key: str) -> int:
    with app.session() as s:
        return category_id(s, key)


def test_one_offs_inside_a_group_budget(config_dir) -> None:
    app = make_app(config_dir, group_budget=True)
    with app.session() as s:
        data = budget_report.budget_vs_actual(s, app.settings, "2026-03", "2026-04")
        regular = budget_report.budget_vs_actual(
            s, app.settings, "2026-03", "2026-04", one_offs=False
        )
    group = {line.key: line for line in data.lines}["wohnen"]
    regular_group = {line.key: line for line in regular.lines}["wohnen"]
    assert group.is_group and not group.one_off
    assert group.spent == 2000_00 + 5000_00
    assert regular_group.spent == 2000_00  # the repairs do not count


# --- configuration -------------------------------------------------------------------


def test_options_are_kept_when_saving_categories(options_app, config_dir) -> None:
    settings = options_app.settings
    config_edit.save_categories(settings, settings.categories)
    lines = (config_dir / "categories.yaml").read_text(encoding="utf-8").splitlines()
    text = "\n".join(line for line in lines if not line.lstrip().startswith("#"))
    assert "report_shift_days: 5" in text and text.count("one_off: true") == 2
    assert "one_off: false" not in text and "report_shift_days: 0" not in text
    reloaded = load_settings(config_dir)
    cats = {f"{g.key}.{c.key}": c for g in reloaded.categories.groups for c in g.categories}
    assert cats[MORTGAGE].report_shift_days == 5 and cats[REPAIRS].one_off
    with options_app.session() as s:
        repairs = s.scalar(select(Category).where(Category.key == REPAIRS))
        mortgage = s.scalar(select(Category).where(Category.key == MORTGAGE))
        assert repairs.one_off and not mortgage.one_off
        assert mortgage.report_shift_days == 5


def test_category_editor_one_off_column(qtbot, options_app) -> None:
    from hiro_budget.gui.config_view import CategoryEditor

    editor = CategoryEditor(options_app)
    qtbot.addWidget(editor)
    tree = editor.tree
    items = {
        f"{tree.topLevelItem(i).text(1)}.{tree.topLevelItem(i).child(j).text(1)}": (
            tree.topLevelItem(i).child(j)
        )
        for i in range(tree.topLevelItemCount())
        for j in range(tree.topLevelItem(i).childCount())
    }
    assert items[REPAIRS].checkState(3) == Qt.CheckState.Checked
    assert items[MORTGAGE].checkState(3) == Qt.CheckState.Unchecked
    items["einkommen.gehalt"].setCheckState(3, Qt.CheckState.Checked)
    assert editor.save_button.isEnabled()
    cats = {f"{g.key}.{c.key}": c for g in editor.to_config().groups for c in g.categories}
    assert cats["einkommen.gehalt"].one_off and cats[REPAIRS].one_off
    assert cats[MORTGAGE].report_shift_days == 5


def test_category_editor_report_month_column(qtbot, options_app) -> None:
    from PySide6.QtWidgets import QSpinBox, QStyleOptionViewItem

    from hiro_budget.gui.config_view import SHIFT_COLUMN, CategoryEditor

    editor = CategoryEditor(options_app)
    qtbot.addWidget(editor)
    tree = editor.tree
    wohnen = next(
        tree.topLevelItem(i)
        for i in range(tree.topLevelItemCount())
        if tree.topLevelItem(i).text(1) == "wohnen"
    )
    mortgage = next(
        wohnen.child(j)
        for j in range(wohnen.childCount())
        if wohnen.child(j).text(1) == "baufinanzierung"
    )
    repairs = next(
        wohnen.child(j)
        for j in range(wohnen.childCount())
        if wohnen.child(j).text(1) == "instandhaltung"
    )
    assert mortgage.text(SHIFT_COLUMN) == "5" and repairs.text(SHIFT_COLUMN) == ""

    delegate = tree.itemDelegateForColumn(SHIFT_COLUMN)
    option = QStyleOptionViewItem()
    group_index = tree.indexFromItem(wohnen, SHIFT_COLUMN)
    assert delegate.createEditor(tree.viewport(), option, group_index) is None  # groups: none
    index = tree.indexFromItem(repairs, SHIFT_COLUMN)
    box = delegate.createEditor(tree.viewport(), option, index)
    assert isinstance(box, QSpinBox) and box.maximum() == 15
    box.setValue(3)
    delegate.setModelData(box, tree.model(), index)
    box.setValue(0)
    delegate.setModelData(box, tree.model(), tree.indexFromItem(mortgage, SHIFT_COLUMN))
    assert repairs.text(SHIFT_COLUMN) == "3" and mortgage.text(SHIFT_COLUMN) == ""
    assert editor.save_button.isEnabled()
    cats = {f"{g.key}.{c.key}": c for g in editor.to_config().groups for c in g.categories}
    assert cats[REPAIRS].report_shift_days == 3 and cats[MORTGAGE].report_shift_days == 0


# --- GUI -----------------------------------------------------------------------------


def category_bars(view) -> list[str]:
    """The names of the slices in "Expenses by category"."""
    return [name for name, _ in view._slices]


def test_reports_switch(qtbot, options_app) -> None:
    from hiro_budget.gui.reports_view import ReportsView

    view = ReportsView(options_app)
    qtbot.addWidget(view)
    view.refresh()  # loads the page (reload waits until it is shown)
    assert view.one_offs.isChecked()
    assert "with one-offs" in view.totals.text()
    assert "Includes one-offs" in view.totals.toolTip()
    assert any(r.endswith("(one-off)") for r in category_bars(view))

    view.one_offs.setChecked(False)
    assert options_app.ui_state.get("reports.one_offs") is False
    assert "without one-offs" in view.totals.text()
    assert "left out" in view.totals.toolTip()
    assert not any(r.endswith("(one-off)") for r in category_bars(view))

    again = ReportsView(options_app)  # remembered
    qtbot.addWidget(again)
    assert not again.one_offs.isChecked()


def test_savings_page_shows_one_offs(qtbot, options_app) -> None:
    from hiro_budget.gui.report_pages import BudgetActualReport, SavingsRateReport

    page = SavingsRateReport(options_app)
    qtbot.addWidget(page)
    page.refresh(date(2026, 3, 1), date(2026, 4, 30), None, one_offs=False)
    assert not page.table.isColumnHidden(6)
    assert "left out" in page.note.text() and "previous month" in page.note.text()
    page.refresh(date(2026, 3, 1), date(2026, 3, 31), None)
    assert page.table.isColumnHidden(6)  # no one-offs in March

    actual = BudgetActualReport(options_app)
    qtbot.addWidget(actual)
    actual.only_budgeted.setChecked(False)
    actual.refresh(date(2026, 3, 1), date(2026, 4, 30), None)
    names = [actual.table.item(r, 0).text() for r in range(actual.table.rowCount())]
    assert any("(one-off)" in n for n in names)
    assert "Includes one-off expenses" in actual.note.text()


def test_export_marks_one_offs(options_app, tmp_path) -> None:
    from hiro_budget.services import export

    with options_app.session() as s:
        months, cats = reports.monthly_totals(s, one_offs=False), reports.category_totals(s)
    keys, rows = export.months_table(months)
    april = dict(zip(keys, rows[-1], strict=True))
    assert april["expenses"] == 1050.0 and april["one_off_expenses"] == 5000.0
    keys, rows = export.categories_table(cats, "de")
    marked = {row[1]: row[keys.index("one_off")] for row in rows}
    assert marked["Instandhaltung & Baumarkt"] == "ja" and marked["Baufinanzierung"] == ""
