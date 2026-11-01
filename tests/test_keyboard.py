"""Keyboard-only use: page switching, focus, shortcuts of the budget and bookings pages."""

from datetime import date

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QAbstractItemView, QApplication, QMenu, QMessageBox

from hiro_budget.gui.budget_view import COL_ASSIGNED, ENV_ROLE
from hiro_budget.gui.main_window import MainWindow
from hiro_budget.gui.shortcuts_dialog import ShortcutsDialog
from hiro_budget.services import import_service
from hiro_budget.services import transactions as txs
from hiro_budget.services.transactions import ManualEntry

CTRL, SHIFT, ALT = (
    Qt.KeyboardModifier.ControlModifier,
    Qt.KeyboardModifier.ShiftModifier,
    Qt.KeyboardModifier.AltModifier,
)


@pytest.fixture(autouse=True)
def no_message_boxes(monkeypatch):
    for name in ("information", "warning", "critical"):
        monkeypatch.setattr(QMessageBox, name, lambda *a, **k: QMessageBox.StandardButton.Ok)


@pytest.fixture
def window(qtbot, app, statement_pdf):
    with app.session() as s:
        import_service.import_file(s, app.settings, statement_pdf)
    w = MainWindow(app)
    qtbot.addWidget(w)
    w.show()
    qtbot.waitExposed(w)
    w.activateWindow()
    w.budget.go("2026-03")
    w.show_page(0)
    return w


def press(qtbot, key, modifier=Qt.KeyboardModifier.NoModifier) -> None:
    qtbot.keyClick(QApplication.focusWidget(), key, modifier)


def test_switch_pages_with_the_keyboard(qtbot, window) -> None:
    tabs = window.tabs
    assert tabs.currentWidget() is window.overview  # the start page
    press(qtbot, Qt.Key.Key_Tab, CTRL)
    assert tabs.currentWidget() is window.budget
    assert QApplication.focusWidget() is window.budget.tree
    press(qtbot, Qt.Key.Key_Tab, CTRL)
    assert tabs.currentWidget() is window.transactions
    assert QApplication.focusWidget() is window.transactions.table  # ready for the arrows
    press(qtbot, Qt.Key.Key_PageDown, CTRL)
    assert tabs.currentWidget() is window.accounts
    press(qtbot, Qt.Key.Key_Backtab, CTRL | SHIFT)
    assert tabs.currentWidget() is window.transactions
    press(qtbot, Qt.Key.Key_PageUp, CTRL)
    press(qtbot, Qt.Key.Key_PageUp, CTRL)
    press(qtbot, Qt.Key.Key_PageUp, CTRL)  # wraps around
    assert tabs.currentWidget() is window.config_view
    press(qtbot, Qt.Key.Key_5, CTRL)
    assert tabs.currentWidget() is window.reports
    press(qtbot, Qt.Key.Key_2, CTRL)
    assert tabs.currentWidget() is window.budget
    press(qtbot, Qt.Key.Key_1, CTRL)
    assert tabs.currentWidget() is window.overview


def test_search_shortcut(qtbot, window) -> None:
    press(qtbot, Qt.Key.Key_F, CTRL)
    view = window.transactions
    assert window.tabs.currentWidget() is view
    assert QApplication.focusWidget() is view.search
    press(qtbot, Qt.Key.Key_Escape)
    assert QApplication.focusWidget() is view.table


def test_budget_keys(qtbot, window) -> None:
    view = window.budget
    window.show_page(window.tabs.indexOf(view))
    press(qtbot, Qt.Key.Key_Right, ALT)
    assert view.month == "2026-04"
    press(qtbot, Qt.Key.Key_Left, ALT)
    assert view.month == "2026-03"

    row = next(
        view.tree.topLevelItem(0).child(i)
        for i in range(view.tree.topLevelItem(0).childCount())
        if view.tree.topLevelItem(0).child(i).data(0, ENV_ROLE) is not None
    )
    env = row.data(0, ENV_ROLE)  # the row itself is rebuilt after saving
    view.tree.setCurrentItem(row)
    press(qtbot, Qt.Key.Key_Return)
    assert view.tree.state() == QAbstractItemView.State.EditingState
    assert view.tree.currentColumn() == COL_ASSIGNED
    qtbot.keyClicks(QApplication.focusWidget(), "75")
    press(qtbot, Qt.Key.Key_Return)  # commits the amount
    qtbot.waitUntil(lambda: view.envelope(env).assigned == 75_00)
    view.setFocus()
    press(qtbot, Qt.Key.Key_Z, CTRL)
    assert view.envelope(env).assigned == 0

    press(qtbot, Qt.Key.Key_Home, ALT)
    assert view.month == max(view.data.start_month, date.today().strftime("%Y-%m"))


def test_bookings_keys(qtbot, window, monkeypatch) -> None:
    from hiro_budget.gui import transactions_view

    shown = []

    class RecordingMenu(QMenu):
        def exec(self, *args):  # instead of waiting for a click
            shown.append(self)

    monkeypatch.setattr(transactions_view, "QMenu", RecordingMenu)
    view = window.transactions
    window.show_page(window.tabs.indexOf(view))
    # counter-bookings and split bookings have no single category to edit
    row = next(
        i for i, r in enumerate(view.model.rows()) if not r.is_counterpart and not r.is_split
    )
    view._select_id(view.model.rows()[row].id)
    press(qtbot, Qt.Key.Key_F2)  # the category menu with its groups
    qtbot.waitUntil(lambda: len(shown) == 1)
    view.table.setFocus()
    press(qtbot, Qt.Key.Key_Return)
    qtbot.waitUntil(lambda: len(shown) == 2)
    assert view.table.state() != QAbstractItemView.State.EditingState  # no editor left open


def test_delete_key_removes_manual_bookings(qtbot, window, monkeypatch) -> None:
    from sqlalchemy import select

    from hiro_budget.db.models import Account

    view = window.transactions
    with window.app.session() as s:
        cash = s.scalar(select(Account.id).where(Account.name == "Bargeld"))
        new_id = txs.create_manual(
            s, window.app.settings, ManualEntry(cash, date(2026, 3, 5), -9_99, "Kiosk")
        )
    window.show_page(window.tabs.indexOf(view))
    view.refresh()
    view._select_id(new_id)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    press(qtbot, Qt.Key.Key_Delete)
    with window.app.session() as s:
        assert all(r.id != new_id for r in txs.list_transactions(s))

    # imported bookings cannot be deleted: nothing happens
    view.table.selectRow(0)
    asked = []
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: asked.append(1))
    if not view.selected_rows()[0].is_manual_entry:
        press(qtbot, Qt.Key.Key_Delete)
        assert asked == []


def test_context_menu_key_and_help(qtbot, window, monkeypatch) -> None:
    from hiro_budget.gui import transactions_view

    shown = []

    class RecordingMenu(QMenu):
        def exec(self, *args):  # instead of waiting for a click
            shown.append(self)

    monkeypatch.setattr(transactions_view, "QMenu", RecordingMenu)
    window.show_page(window.tabs.indexOf(window.transactions))
    window.transactions.table.selectRow(0)
    press(qtbot, Qt.Key.Key_F10, SHIFT)
    assert shown  # the right-click menu of the selected booking

    opened = []
    monkeypatch.setattr(ShortcutsDialog, "exec", lambda dlg: opened.append(dlg))
    press(qtbot, Qt.Key.Key_F1)
    assert len(opened) == 1
