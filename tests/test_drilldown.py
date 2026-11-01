"""Breakdowns of report numbers (services.drilldown) and the breakdown dialog."""

from datetime import date

import pytest
from PySide6.QtWidgets import QMessageBox

from hiro_budget.services import budget_report, drilldown, reports
from tests.test_report_options import MORTGAGE, REPAIRS, category_id, make_app


@pytest.fixture(autouse=True)
def no_message_boxes(monkeypatch):
    for name in ("information", "warning", "critical"):
        monkeypatch.setattr(QMessageBox, name, lambda *a, **k: QMessageBox.StandardButton.Ok)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.No)


@pytest.fixture
def app(config_dir):
    """Bargeld, April 2026: mortgage on 02.04. (counts for March) and 06.04., groceries,
    repairs (one-off), a refund (one-off income) and salary."""
    return make_app(config_dir)


APRIL = (date(2026, 4, 1), date(2026, 4, 30))


def test_breakdown_adds_up_to_the_monthly_totals(app) -> None:
    with app.session() as s:
        for one_offs in (True, False):
            totals = {m.month: m for m in reports.monthly_totals(s, one_offs=one_offs)}
            april = totals["2026-04"]
            for measure, expected in (
                ("income", april.income_cents),
                ("expenses", april.expense_cents),
                ("saved", april.net_cents),
            ):
                b = drilldown.report_breakdown(s, measure, *APRIL, one_offs=one_offs)
                assert b.total == expected, (measure, one_offs)
        regular = drilldown.report_breakdown(s, "expenses", *APRIL, one_offs=False)
        both = drilldown.report_breakdown(s, "saved", *APRIL)
    assert [p.key for p in regular.left_out] == [REPAIRS]
    assert regular.left_out_cents == 5000_00
    assert [s.kind for s in both.sections] == ["income", "expenses"]
    expenses = {p.key: p for p in both.sections[1].parts}
    assert list(expenses)[0] == REPAIRS  # largest first
    assert expenses[REPAIRS].one_off and expenses[REPAIRS].bookings[0].payee == "Dachdecker"


def test_shifted_bookings_are_marked(app) -> None:
    with app.session() as s:
        march = drilldown.report_breakdown(s, "expenses", date(2026, 3, 1), date(2026, 3, 31))
        april = drilldown.report_breakdown(
            s, "expenses", *APRIL, category_ids=[category_id(s, MORTGAGE)]
        )
    (part,) = march.sections[0].parts
    (booking,) = part.bookings
    assert part.key == MORTGAGE and booking.booking_date == date(2026, 4, 2)
    assert booking.shifted and booking.report_month == "2026-03"
    (part,) = april.sections[0].parts
    assert [b.booking_date for b in part.bookings] == [date(2026, 4, 6)]
    assert not part.bookings[0].shifted


def test_category_and_one_off_breakdowns(app) -> None:
    with app.session() as s:
        for t in reports.category_totals(s):
            b = drilldown.report_breakdown(s, "expenses", None, None, category_ids=[t.category_id])
            assert b.total == t.expense_cents
        one_offs = drilldown.report_breakdown(s, "saved", *APRIL, only_one_offs=True)
    assert one_offs.total == 300_00 - 5000_00


def test_budget_breakdown_matches_budget_vs_actual(app) -> None:
    with app.session() as s:
        for one_offs in (True, False):
            data = budget_report.budget_vs_actual(
                s, app.settings, "2026-03", "2026-04", one_offs=one_offs
            )
            months = [m.month for m in data.months]
            for line in data.lines:
                b = drilldown.budget_breakdown(s, app.settings, months, line.category_ids)
                assert b.total == line.spent, line.key
            for m in data.months:
                b = drilldown.budget_breakdown(s, app.settings, [m.month], data.category_ids)
                assert b.total == m.spent
            if not one_offs:
                assert category_id(s, REPAIRS) not in data.category_ids


# --- GUI -------------------------------------------------------------------------------


def test_savings_page_breakdown(qtbot, app) -> None:
    from hiro_budget.gui.report_pages import SavingsRateReport

    page = SavingsRateReport(app)
    qtbot.addWidget(page)
    page.refresh(date(2026, 3, 1), date(2026, 4, 30), None, one_offs=False)
    row = [m.month for m in page.data.months].index("2026-04")
    page._drill(row, 2)  # expenses of April
    dialog = page.breakdown_dialog
    assert dialog.breakdown.total == page.data.months[row].expense_cents == 1050_00
    assert page.table.item(row, 2).text().startswith("1")  # the number in the table
    assert "Instandhaltung" in dialog.notes.text()  # left out, with its amount
    assert "1 booking" in dialog.tree.topLevelItem(0).text(1) or dialog.tree.topLevelItemCount()

    page._drill(len(page.data.months), 3)  # saved, period row: income and expenses
    dialog = page.breakdown_dialog
    assert dialog.tree.topLevelItemCount() == 2 and dialog.breakdown.total == page.data.saved_cents

    page._drill(row, 6)  # one-offs of April, whatever the switch
    assert page.breakdown_dialog.breakdown.total == 300_00 - 5000_00


def test_dialog_opens_a_booking(qtbot, app) -> None:
    from hiro_budget.gui.report_pages import SavingsRateReport

    page = SavingsRateReport(app)
    qtbot.addWidget(page)
    page.refresh(date(2026, 4, 1), date(2026, 4, 30), None)
    page._drill(0, 2)
    dialog = page.breakdown_dialog
    category = dialog.tree.topLevelItem(0)
    booking = category.child(0)
    dialog.tree.setCurrentItem(category)
    assert not dialog.show_button.isEnabled()  # a category is no booking
    dialog.tree.setCurrentItem(booking)
    assert dialog.show_button.isEnabled()
    with qtbot.waitSignal(page.show_booking) as signal:
        dialog.show_button.click()
    assert signal.args == [dialog.breakdown.sections[0].parts[0].bookings[0].tx_id]
    assert not dialog.isVisible()


def test_budget_and_year_pages(qtbot, app) -> None:
    from hiro_budget.gui.report_pages import BudgetActualReport, YearComparisonReport

    actual = BudgetActualReport(app)
    qtbot.addWidget(actual)
    actual.only_budgeted.setChecked(False)
    actual.refresh(date(2026, 3, 1), date(2026, 4, 30), None)
    names = [actual.table.item(r, 0).text() for r in range(actual.table.rowCount())]
    row = next(i for i, n in enumerate(names) if "Baufinanzierung" in n)
    actual._drill(row, 2)
    assert actual.breakdown_dialog.breakdown.total == actual.data.lines[row].spent == 2000_00
    actual._drill(len(actual.data.lines), 2)  # the total row
    assert actual.breakdown_dialog.breakdown.total == actual.data.spent

    years = YearComparisonReport(app)
    qtbot.addWidget(years)
    years.refresh(date(2026, 1, 1), date(2026, 12, 31), None)
    years.same_period.setChecked(False)
    rows = years.data.rows
    years._drill(0, 2)  # largest category, second year
    assert years.breakdown_dialog.breakdown.total == rows[0].second
    years._drill(len(rows), 2)  # income
    assert years.breakdown_dialog.breakdown.total == years.data.income[1]


def test_overview_tab_and_main_window(qtbot, app) -> None:
    from hiro_budget.gui.main_window import MainWindow

    window = MainWindow(app)
    qtbot.addWidget(window)
    view = window.reports
    view.refresh()
    # the donut: a slice opens its category's expenses
    first = view._cats[0]
    (donut,) = view.category_view.chart().series()
    index = next(i for i, (_, cats) in enumerate(view._slices) if cats == [first])
    donut.slices()[index].doubleClicked.emit()
    assert view.breakdown_dialog.breakdown.total == first.expense_cents
    assert sum(s.value() for s in donut.slices()) * 100 == pytest.approx(
        sum(c.expense_cents for c in view._cats if c.expense_cents > 0)
    )
    assert len(view._legend) == len(view._slices) and view._center[1].text() == "expenses"
    # fixed + variable costs add up to each month's expenses; a bar opens the expenses
    (costs,) = view.costs_view.chart().series()
    fixed, variable = costs.barSets()
    for i, m in enumerate(view.months):
        assert round((fixed.at(i) + variable.at(i)) * 100) == m.expense_cents
    variable.doubleClicked.emit(0)
    assert view.breakdown_dialog.breakdown.total == view.months[0].expense_cents
    view._drill_month("2026-04", "saved")
    month = next(m for m in view.months if m.month == "2026-04")
    assert view.breakdown_dialog.breakdown.total == month.net_cents
    tx_id = view.breakdown_dialog.breakdown.sections[0].parts[0].bookings[0].tx_id
    view.show_booking.emit(tx_id)
    assert window.tabs.currentWidget() is window.transactions
