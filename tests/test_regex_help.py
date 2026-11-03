"""Regex cheat sheet (#30): "?" in regex fields, F1 there, Help menu window, copy, examples."""

import re

import pytest
from PySide6.QtCore import QCoreApplication
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication, QLineEdit, QMessageBox

from hiro_budget.gui import main_window as mw
from hiro_budget.gui import regex_help
from hiro_budget.gui.main_window import MainWindow
from hiro_budget.gui.regex_help import RegexHelpPopup, RegexHelpWindow


@pytest.mark.parametrize("ex", regex_help.examples(), ids=lambda e: e.pattern)
def test_every_example_does_what_it_says(ex) -> None:
    rx = re.compile(ex.pattern)  # rules compile and search like this (rules.py)
    assert rx.search(ex.matches), ex.matches
    assert not rx.search(ex.misses), ex.misses


@pytest.fixture(autouse=True)
def no_message_boxes(monkeypatch):
    for name in ("information", "warning", "critical"):
        monkeypatch.setattr(QMessageBox, name, lambda *a, **k: QMessageBox.StandardButton.Ok)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.No)


@pytest.fixture
def window(qtbot, app):
    w = MainWindow(app)
    qtbot.addWidget(w)
    w.show()
    yield w
    popup = QApplication.activePopupWidget()
    if popup is not None:
        popup.close()


def regex_fields(page) -> list[QLineEdit]:
    return [e for e in page.findChildren(QLineEdit) if e.property(regex_help.REGEX_HELP)]


def help_action(edit: QLineEdit):
    return next(a for a in edit.actions() if a.objectName() == "regexHelpAction")


def open_popup(qtbot, edit: QLineEdit) -> RegexHelpPopup:
    help_action(edit).trigger()
    qtbot.waitUntil(lambda: isinstance(QApplication.activePopupWidget(), RegexHelpPopup))
    return QApplication.activePopupWidget()


def test_rule_editor_regex_fields_have_help(window) -> None:
    page = window.config_view.rules
    fields = regex_fields(page)
    assert set(fields) == {
        page.inputs[n]
        for n in ("counterparty", "merchant", "purpose", "booking_type", "details", "account")
    }
    assert not page.inputs["amount_min"].property(regex_help.REGEX_HELP)


def test_question_mark_opens_the_sheet_next_to_the_field(window, qtbot) -> None:
    window.tabs.setCurrentWidget(window.config_view)
    edit = window.config_view.rules.inputs["merchant"]
    popup = open_popup(qtbot, edit)
    assert len(popup.sheet.copy_buttons) == len(regex_help.examples())
    popup.close()


def test_account_editor_fields_have_help(window, qtbot) -> None:
    window.tabs.setCurrentWidget(window.accounts)
    acc = window.accounts
    assert set(regex_fields(acc)) == {acc.counterparty, acc.purpose}
    open_popup(qtbot, acc.purpose).close()


def test_f1_in_a_regex_field_opens_the_sheet(window, qtbot, monkeypatch) -> None:
    shown = []
    monkeypatch.setattr(mw.ShortcutsDialog, "exec", lambda self: shown.append(self))
    window.tabs.setCurrentWidget(window.config_view)
    edit = window.config_view.rules.inputs["purpose"]
    monkeypatch.setattr(QApplication, "focusWidget", staticmethod(lambda: edit))
    window.show_shortcuts()
    qtbot.waitUntil(lambda: isinstance(QApplication.activePopupWidget(), RegexHelpPopup))
    QApplication.activePopupWidget().close()
    assert shown == []
    # anywhere else F1 is still the keyboard shortcuts overview
    other = window.config_view.rules.inputs["id"]
    monkeypatch.setattr(QApplication, "focusWidget", staticmethod(lambda: other))
    window.show_shortcuts()
    assert len(shown) == 1


def test_help_menu_opens_one_window_that_does_not_block(window) -> None:
    action = next(
        a for menu in window._menus for a in menu.actions() if a.text() == "&Regex cheat sheet…"
    )
    action.trigger()
    first = window._regex_window
    assert isinstance(first, RegexHelpWindow)
    assert first.isVisible() and not first.isModal()
    first.close()
    action.trigger()
    assert window._regex_window is first and first.isVisible()
    first.close()


def test_copy_puts_the_pattern_on_the_clipboard(window) -> None:
    window.show_regex_help()
    sheet = window._regex_window.sheet
    pattern = regex_help.examples()[1].pattern
    sheet.copy_buttons[pattern].click()
    assert QGuiApplication.clipboard().text() == pattern
    window._regex_window.close()


def test_german(qtbot) -> None:
    from hiro_budget.gui.main import install_translators

    translators = install_translators(QApplication.instance(), "de")
    try:
        w = RegexHelpWindow()
        qtbot.addWidget(w)
        assert w.windowTitle() == "Regex-Spickzettel"
        assert regex_help.examples()[2].meaning == "Mit oder ohne Bindestrich"
    finally:
        for t in translators:
            QCoreApplication.removeTranslator(t)
