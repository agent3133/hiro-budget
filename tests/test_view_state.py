"""View settings remembered between sessions (ui_state.json next to the database)."""

import json

import pytest
from PySide6.QtWidgets import QMessageBox

from hiro_budget.gui.accounts_view import AccountsView
from hiro_budget.gui.budget_view import ALL, COL_NAME, GROUP_ROLE, OVERSPENT, BudgetView
from hiro_budget.gui.main_window import MainWindow
from hiro_budget.services import import_service
from hiro_budget.ui_state import UiState


@pytest.fixture(autouse=True)
def no_message_boxes(monkeypatch):
    for name in ("information", "warning", "critical"):
        monkeypatch.setattr(QMessageBox, name, lambda *a, **k: QMessageBox.StandardButton.Ok)


def test_ui_state_file(tmp_path) -> None:
    path = tmp_path / "data" / "ui_state.json"
    state = UiState(path)
    assert state.get("x", 1) == 1
    state.set("x", 2)
    state.set("names", ["a", "b"])
    assert UiState(path).get("x", 1) == 2
    assert UiState(path).get("names", []) == ["a", "b"]
    assert UiState(path).get("x", "text") == "text"  # wrong type: default
    path.write_text("{broken", encoding="utf-8")
    assert UiState(path).get("x", 1) == 1  # unreadable file: defaults


def test_state_lives_next_to_the_database(app) -> None:
    app.ui_state.set("probe", True)
    assert app.ui_state.path == app.settings.db_path.parent / "ui_state.json"
    assert json.loads(app.ui_state.path.read_text(encoding="utf-8"))["probe"] is True


def test_budget_view_remembers_settings(qtbot, app, statement_pdf) -> None:
    with app.session() as s:
        import_service.import_file(s, app.settings, statement_pdf)
    view = BudgetView(app)
    qtbot.addWidget(view)
    view.go("2026-03")
    view.hide_empty.setChecked(False)
    view.filter.setCurrentIndex(view.filter.findData(OVERSPENT))
    assert BudgetView(app).filter.currentData() == OVERSPENT
    view.filter.setCurrentIndex(view.filter.findData(ALL))
    first = view.tree.topLevelItem(0)
    key = first.data(COL_NAME, GROUP_ROLE)
    first.setExpanded(False)

    again = BudgetView(app)
    qtbot.addWidget(again)
    again.go("2026-03")
    assert again.filter.currentData() == ALL
    assert not again.hide_empty.isChecked()
    assert key in again.collapsed_groups
    assert not again.tree.topLevelItem(0).isExpanded()
    assert again.tree.topLevelItem(1).isExpanded()

    again.expand_button.click()  # collapse all ...
    again.expand_button.click()  # ... and expand all again
    assert BudgetView(app).collapsed_groups == set()


def test_accounts_view_remembers_collapsed_depot(qtbot, app) -> None:
    app.ui_state.set("accounts.collapsed", ["ING Direkt-Depot"])
    view = AccountsView(app)
    qtbot.addWidget(view)
    assert view.collapsed == {"ING Direkt-Depot"}


def test_main_window_remembers_tab_and_columns(qtbot, app) -> None:
    w = MainWindow(app)
    qtbot.addWidget(w)
    w.tabs.setCurrentWidget(w.accounts)
    w.transactions.table.setColumnWidth(6, 333)  # category (not the flexible column)
    w.save_state()

    again = MainWindow(app)
    qtbot.addWidget(again)
    assert again.tabs.currentWidget() is again.accounts
    assert again.transactions.table.columnWidth(6) == 333
