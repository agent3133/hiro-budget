"""F6 areas, the bookings filters and card by keyboard, period steps (#69)."""

from datetime import date

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QDateEdit, QMessageBox

from hiro_budget.gui.common import from_qdate, to_qdate
from hiro_budget.gui.date_range import DateRangeButton
from hiro_budget.services import transactions as txs
from tests.test_keyboard import ALT, CTRL, SHIFT, no_message_boxes, press, window  # noqa: F401


@pytest.fixture(autouse=True)
def quit_anyway(monkeypatch):
    """Ctrl+R starts a rule: the window asks about it when it is closed."""
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)


def release(qtbot) -> None:
    for key in (Qt.Key.Key_Control, Qt.Key.Key_Shift, Qt.Key.Key_Alt):
        qtbot.keyRelease(QApplication.focusWidget(), key)


def bookings(window):  # noqa: F811
    view = window.transactions
    window.show_page(window.tabs.indexOf(view))
    row = next(r for r in view.model.rows() if not r.is_counterpart and not r.is_split)
    view._select_id(row.id)
    view.table.setFocus()
    return view


def test_f6_goes_round_the_areas(qtbot, window) -> None:  # noqa: F811
    view = bookings(window)
    press(qtbot, Qt.Key.Key_F6)
    assert QApplication.focusWidget() is view.card.category
    press(qtbot, Qt.Key.Key_F6)
    assert QApplication.focusWidget() is window.tabs.list  # the sidebar
    press(qtbot, Qt.Key.Key_F6)
    assert QApplication.focusWidget() is view.search
    press(qtbot, Qt.Key.Key_F6, SHIFT)
    assert QApplication.focusWidget() is window.tabs.list
    assert window.tabs.currentWidget() is view  # the page stays
    release(qtbot)


def test_quick_filters_and_clearing(qtbot, window) -> None:  # noqa: F811
    view = bookings(window)
    press(qtbot, Qt.Key.Key_2, ALT)
    assert view.current_filter().category == txs.UNCATEGORIZED
    press(qtbot, Qt.Key.Key_1, ALT)
    assert view.current_filter().category == txs.ALL
    view.search.setText("rewe")
    view.table.setFocus()
    press(qtbot, Qt.Key.Key_F, CTRL | SHIFT)
    assert view.search.text() == ""
    release(qtbot)


def test_card_keys(qtbot, window) -> None:  # noqa: F811
    view = bookings(window)
    press(qtbot, Qt.Key.Key_E, CTRL)
    assert QApplication.focusWidget() is view.notes
    press(qtbot, Qt.Key.Key_Escape)
    assert QApplication.focusWidget() is view.table
    with qtbot.waitSignal(view.card.rule_requested):
        press(qtbot, Qt.Key.Key_R, CTRL)
    release(qtbot)


def test_period_steps_on_bookings_and_reports(qtbot, window) -> None:  # noqa: F811
    view = bookings(window)
    view.period._choose((date(2026, 3, 1), date(2026, 3, 31)))
    press(qtbot, Qt.Key.Key_PageUp, ALT)
    assert view.current_filter().date_from == date(2026, 2, 1)
    assert view.current_filter().date_to == date(2026, 2, 28)
    press(qtbot, Qt.Key.Key_5, CTRL)
    reports = window.reports
    reports.period._choose((date(2025, 1, 1), date(2025, 12, 31)))
    press(qtbot, Qt.Key.Key_PageDown, ALT)
    assert from_qdate(reports.date_from.date()) == date(2026, 1, 1)
    assert from_qdate(reports.date_to.date()) == date(2026, 12, 31)
    release(qtbot)


def test_step_by_months_years_and_days(qtbot) -> None:
    first, last = QDateEdit(), QDateEdit()
    button = DateRangeButton(first, last, lambda: None)
    qtbot.addWidget(button)

    def span(a: date, b: date, n: int) -> tuple[date, date]:
        first.setDate(to_qdate(a))
        last.setDate(to_qdate(b))
        button.step(n)
        return from_qdate(first.date()), from_qdate(last.date())

    assert span(date(2026, 1, 1), date(2026, 1, 31), 1) == (date(2026, 2, 1), date(2026, 2, 28))
    assert span(date(2025, 11, 1), date(2026, 1, 31), 1) == (date(2026, 2, 1), date(2026, 4, 30))
    assert span(date(2025, 1, 1), date(2025, 12, 31), -1) == (date(2024, 1, 1), date(2024, 12, 31))
    assert span(date(2026, 3, 5), date(2026, 3, 11), 1) == (date(2026, 3, 12), date(2026, 3, 18))
