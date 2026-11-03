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
    press(qtbot, Qt.Key.Key_PageDown, CTRL)  # tabs within a page only (#67)
    assert tabs.currentWidget() is window.transactions
    press(qtbot, Qt.Key.Key_Tab, CTRL)
    assert tabs.currentWidget() is window.accounts
    press(qtbot, Qt.Key.Key_Backtab, CTRL | SHIFT)
    assert tabs.currentWidget() is window.transactions
    press(qtbot, Qt.Key.Key_Backtab, CTRL | SHIFT)
    press(qtbot, Qt.Key.Key_Backtab, CTRL | SHIFT)
    press(qtbot, Qt.Key.Key_Backtab, CTRL | SHIFT)  # wraps around
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
    press(qtbot, Qt.Key.Key_PageDown, ALT)
    assert view.month == "2026-04"
    press(qtbot, Qt.Key.Key_PageUp, ALT)
    assert view.month == "2026-03"
    press(qtbot, Qt.Key.Key_Up, CTRL | SHIFT)  # collapse all groups
    assert not any(g.isExpanded() for g in view._groups())
    press(qtbot, Qt.Key.Key_Down, CTRL | SHIFT)
    assert all(g.isExpanded() for g in view._groups())
    qtbot.keyRelease(QApplication.focusWidget(), Qt.Key.Key_Control)

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


def test_every_bound_key_is_in_the_overview(qtbot, window) -> None:
    """Each QShortcut and QAction key of the window (all pages built) is in the
    shortcut table, so F1 cannot miss a key (#65)."""
    from PySide6.QtGui import QAction, QKeySequence, QShortcut

    from hiro_budget.gui import shortcuts

    for name in window.PAGES:
        window._page(name)
    known = {
        seq.toString()
        for entry in shortcuts.TABLE
        if not entry.hidden
        for seq in shortcuts.sequences(entry.id)
    }
    found = {s.key().toString() for s in window.findChildren(QShortcut)}
    found |= {
        k.toString() for a in window.findChildren(QAction) for k in a.shortcuts() if not k.isEmpty()
    }
    mnemonics = {
        QKeySequence.mnemonic(m.title()).toString() for m in window._menus
    }  # "Alt + first letter"
    assert sorted(found - known - mnemonics - {""}) == []


def test_alt_left_is_back_on_the_budget_too(qtbot, window) -> None:
    """Alt+Left goes back, also on the budget (it changed the month before #66)."""
    view = window.budget
    window.show_page(window.tabs.indexOf(view))
    press(qtbot, Qt.Key.Key_Left, ALT)
    assert window.tabs.currentWidget() is window.overview
    assert view.month == "2026-03"
    qtbot.keyRelease(QApplication.focusWidget(), Qt.Key.Key_Alt)


def test_tabs_within_a_page(qtbot, window) -> None:
    """Ctrl+PgDn / PgUp: report tabs and categories <-> rules; Alt+n: report n (#67)."""
    press(qtbot, Qt.Key.Key_5, CTRL)
    reports = window.reports
    assert reports.tabs.currentIndex() == 0
    press(qtbot, Qt.Key.Key_PageDown, CTRL)
    assert reports.tabs.currentIndex() == 1
    assert window.tabs.currentWidget() is reports  # still the reports page
    press(qtbot, Qt.Key.Key_PageUp, CTRL)
    press(qtbot, Qt.Key.Key_PageUp, CTRL)  # round to the last report
    assert reports.tabs.currentIndex() == reports.tabs.count() - 1
    press(qtbot, Qt.Key.Key_3, ALT)
    assert reports.tabs.currentIndex() == 2

    press(qtbot, Qt.Key.Key_6, CTRL)
    config = window.config_view
    assert QApplication.focusWidget() is config.categories.tree
    press(qtbot, Qt.Key.Key_PageDown, CTRL)
    assert QApplication.focusWidget() is config.rules.list
    press(qtbot, Qt.Key.Key_PageUp, CTRL)
    assert QApplication.focusWidget() is config.categories.tree
    for key in (Qt.Key.Key_Control, Qt.Key.Key_Alt):  # not "held" for later tests
        qtbot.keyRelease(QApplication.focusWidget(), key)


def test_global_keys(qtbot, window) -> None:
    """Fetch, save all, export, user guide, light / dark (#71)."""
    from PySide6.QtGui import QKeySequence

    called = []
    for keys in ("Ctrl+Shift+I", "Ctrl+Shift+S", "Ctrl+Shift+E", "Shift+F1"):
        action = next(a for a in window.actions() if a.shortcut() == QKeySequence(keys))
        action.triggered.disconnect()  # no dialog: record instead
        action.triggered.connect(lambda _=False, keys=keys: called.append(keys))
    theme = window.app.settings.app.theme
    for key in (Qt.Key.Key_I, Qt.Key.Key_S, Qt.Key.Key_E):
        press(qtbot, key, CTRL | SHIFT)
    press(qtbot, Qt.Key.Key_F1, SHIFT)
    assert called == ["Ctrl+Shift+I", "Ctrl+Shift+S", "Ctrl+Shift+E", "Shift+F1"]
    press(qtbot, Qt.Key.Key_L, CTRL | SHIFT)
    first = window.app.settings.app.theme
    press(qtbot, Qt.Key.Key_L, CTRL | SHIFT)
    assert theme != first != window.app.settings.app.theme  # light <-> dark
    for key in (Qt.Key.Key_Control, Qt.Key.Key_Shift):
        qtbot.keyRelease(QApplication.focusWidget(), key)
