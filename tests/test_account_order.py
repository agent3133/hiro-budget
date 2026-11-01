"""Accounts tab: the saved order (drag and drop, up/down) and the temporary column sort."""

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QMessageBox

from hiro_budget.config import load_settings
from hiro_budget.gui.accounts_view import COL_BALANCE, COL_NAME, Drop
from hiro_budget.gui.main_window import MainWindow
from hiro_budget.services import import_service


@pytest.fixture(autouse=True)
def no_message_boxes(monkeypatch):
    for name in ("information", "warning", "critical"):
        monkeypatch.setattr(QMessageBox, name, lambda *a, **k: QMessageBox.StandardButton.Ok)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.No)


@pytest.fixture
def acc(qtbot, app, statement_pdf):
    with app.session() as s:
        import_service.import_file(s, app.settings, statement_pdf)
    w = MainWindow(app)
    qtbot.addWidget(w)
    return w.accounts


def names(acc) -> list[str]:
    return [d.name for d in acc.drafts]


def rows(acc) -> list[str]:
    return [acc.table.topLevelItem(i).text(COL_NAME) for i in range(acc.table.topLevelItemCount())]


def sidebar(acc) -> list[str]:
    return [name for name, _ in acc.balance_list()]


def test_up_down_change_the_saved_order(acc, config_dir) -> None:
    before = names(acc)
    acc.select_account(before[0])
    assert not acc.up_button.isEnabled() and acc.down_button.isEnabled()
    acc.down_button.click()
    after = [before[1], before[0], *before[2:]]
    assert names(acc) == rows(acc) == after
    assert acc._current().name == before[0]  # the moved account stays selected
    assert acc.has_unsaved_changes()
    assert sidebar(acc) == [n for n in before if n in sidebar(acc)]  # not saved yet
    acc.save()
    assert [a.name for a in load_settings(config_dir).app.own_accounts] == after
    assert names(acc) == after and sidebar(acc) == [n for n in after if n in sidebar(acc)]


def test_revert_restores_the_order(acc) -> None:
    before = names(acc)
    acc.select_account(before[-1])
    acc._move(-1)
    assert names(acc) != before
    acc.revert_button.click()
    assert names(acc) == before and not acc.has_unsaved_changes()


def test_drag_and_drop(acc) -> None:
    before = names(acc)
    assert len(before) >= 3
    first, third = acc._item_of(0), acc._item_of(2)
    acc._dropped(first, third, Drop.BelowItem)
    assert names(acc) == [before[1], before[2], before[0], *before[3:]]
    acc._dropped(acc._item_of(2), acc._item_of(0), Drop.AboveItem)
    assert names(acc) == before
    acc._dropped(acc._item_of(0), acc._item_of(1), Drop.AboveItem)  # where it already is
    assert names(acc) == before


def test_column_sort_is_only_a_view(acc) -> None:
    before = names(acc)
    acc._header_clicked(COL_BALANCE)
    balances = {d.name: d.balance.balance_cents for d in acc.drafts if d.balance}
    shown = [r for r in rows(acc) if r in balances]
    assert [balances[n] for n in shown] == sorted(balances[n] for n in shown)
    assert names(acc) == before and not acc.has_unsaved_changes()
    header = acc.table.header()
    assert (
        header.isSortIndicatorShown() and header.sortIndicatorOrder() == Qt.SortOrder.AscendingOrder
    )
    # no reordering while sorted
    assert not acc.up_button.isEnabled() and not acc.down_button.isEnabled()
    assert not acc.table.dragEnabled()
    acc._move(1)
    assert names(acc) == before

    # editing works on the account the row shows
    top = acc.table.topLevelItem(0)
    acc.table.setCurrentItem(top)
    shown_name = top.text(COL_NAME)
    assert acc._current().name == shown_name
    acc.name.setText("Umbenannt")
    acc.name.textEdited.emit("Umbenannt")
    assert top.text(COL_NAME) == "Umbenannt"
    assert names(acc)[before.index(shown_name)] == "Umbenannt"

    acc._header_clicked(COL_BALANCE)  # descending
    assert header.sortIndicatorOrder() == Qt.SortOrder.DescendingOrder
    acc._header_clicked(COL_BALANCE)  # back to the saved order
    assert not header.isSortIndicatorShown()
    assert rows(acc) == names(acc)
    acc.select_account(names(acc)[0])
    assert acc.down_button.isEnabled() and acc.table.dragEnabled()
