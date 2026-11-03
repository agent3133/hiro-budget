"""The same keys on every list: new, remove, move, edit, revert (#68)."""

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QMessageBox

from tests.test_keyboard import ALT, CTRL, SHIFT, press, window  # noqa: F401 (fixture)


@pytest.fixture(autouse=True)
def answer_yes(monkeypatch):
    """The revert key asks first."""
    asked = []

    def question(*args, **kwargs):
        asked.append(args)
        return QMessageBox.StandardButton.Yes

    monkeypatch.setattr(QMessageBox, "question", question)
    return asked


def release(qtbot) -> None:
    for key in (Qt.Key.Key_Control, Qt.Key.Key_Shift, Qt.Key.Key_Alt):
        qtbot.keyRelease(QApplication.focusWidget(), key)


def test_accounts(qtbot, window, answer_yes) -> None:  # noqa: F811
    press(qtbot, Qt.Key.Key_4, CTRL)
    view = window.accounts
    assert QApplication.focusWidget() is view.table
    n = len(view.drafts)
    press(qtbot, Qt.Key.Key_N, CTRL | SHIFT)
    assert len(view.drafts) == n + 1
    assert QApplication.focusWidget() is view.name  # ready to type the name
    view.table.setFocus()
    press(qtbot, Qt.Key.Key_Up, ALT)  # the new account one up
    assert view._current_index() == n - 1
    press(qtbot, Qt.Key.Key_Delete)
    assert len(view.drafts) == n
    press(qtbot, Qt.Key.Key_F2)
    assert QApplication.focusWidget() is view.name
    press(qtbot, Qt.Key.Key_N, CTRL | SHIFT)
    assert view.revert_button.isEnabled()
    press(qtbot, Qt.Key.Key_R, CTRL | SHIFT)
    assert len(answer_yes) == 1
    assert len(view.drafts) == n
    assert not view.revert_button.isEnabled()
    release(qtbot)


def test_ctrl_n_on_accounts_books_in_the_selected_account(qtbot, window, monkeypatch) -> None:  # noqa: F811
    opened = []
    monkeypatch.setattr(window.transactions, "new_booking", lambda *a: opened.append(a))
    press(qtbot, Qt.Key.Key_4, CTRL)
    view = window.accounts
    view.table.setCurrentIndex(view.table.model().index(0, 0))
    press(qtbot, Qt.Key.Key_N, CTRL)
    assert opened == [(view._current().balance.account_id,)]
    release(qtbot)


def test_categories(qtbot, window, answer_yes) -> None:  # noqa: F811
    press(qtbot, Qt.Key.Key_6, CTRL)
    editor = window.config_view.categories
    tree = editor.tree
    assert QApplication.focusWidget() is tree
    groups = tree.topLevelItemCount()
    press(qtbot, Qt.Key.Key_G, CTRL | SHIFT)
    assert tree.topLevelItemCount() == groups + 1
    press(qtbot, Qt.Key.Key_Escape)  # leaves the name as it is
    qtbot.waitUntil(tree.hasFocus)
    new = tree.currentItem()
    index = tree.indexOfTopLevelItem(new)
    press(qtbot, Qt.Key.Key_Up, ALT)
    assert tree.indexOfTopLevelItem(new) == index - 1
    press(qtbot, Qt.Key.Key_N, CTRL | SHIFT)  # a category in the new group
    assert new.childCount() == 1
    press(qtbot, Qt.Key.Key_Escape)
    qtbot.waitUntil(tree.hasFocus)
    press(qtbot, Qt.Key.Key_Delete)
    assert new.childCount() == 0
    press(qtbot, Qt.Key.Key_R, CTRL | SHIFT)
    assert tree.topLevelItemCount() == groups
    assert not editor.revert_button.isEnabled()
    release(qtbot)


def test_rules(qtbot, window) -> None:  # noqa: F811
    press(qtbot, Qt.Key.Key_6, CTRL)
    press(qtbot, Qt.Key.Key_PageDown, CTRL)
    rules = window.config_view.rules
    assert QApplication.focusWidget() is rules.list
    n = rules.list.count()
    press(qtbot, Qt.Key.Key_N, CTRL | SHIFT)
    assert rules.list.count() == n + 1
    assert QApplication.focusWidget() is rules.inputs["id"]
    rules.list.setFocus()
    press(qtbot, Qt.Key.Key_F2)
    assert QApplication.focusWidget() is rules.inputs["counterparty"]
    rules.list.setFocus()
    press(qtbot, Qt.Key.Key_Delete)
    assert rules.list.count() == n
    release(qtbot)


def test_split_parts(qtbot, window) -> None:  # noqa: F811
    view = window.transactions
    window.show_page(window.tabs.indexOf(view))
    row = next(r for r in view.model.rows() if not r.is_counterpart and not r.is_split)
    view._select_id(row.id)
    press(qtbot, Qt.Key.Key_D, CTRL)  # split: the editor in the card
    editor = view.card.editor
    qtbot.waitUntil(editor.isVisible)
    parts = len(editor.parts)
    editor.parts[0].combo.setFocus()
    press(qtbot, Qt.Key.Key_N, CTRL | SHIFT)
    assert len(editor.parts) == parts + 1
    assert QApplication.focusWidget() is editor.parts[-1].combo
    release(qtbot)
