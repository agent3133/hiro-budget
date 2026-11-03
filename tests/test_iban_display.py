"""IBANs are shown in groups of four and stored without spaces (#14)."""

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QMessageBox

from hiro_budget.config import load_settings
from hiro_budget.gui.common import format_iban, format_iban_input
from hiro_budget.gui.main_window import MainWindow

GIRO = "DE12345678901234567890"  # the test config's IBAN, normalized


@pytest.mark.parametrize(
    ("text", "shown"),
    [
        (GIRO, "DE12 3456 7890 1234 5678 90"),
        ("de12 3456 78901234567890", "DE12 3456 7890 1234 5678 90"),  # typed or pasted
        ("NO0086011117947", "NO00 8601 1117 947"),  # the shortest IBAN (Norway, 15)
        ("MT00MALT011000012345MTLCAST001S", "MT00 MALT 0110 0001 2345 MTLC AST0 01S"),
        ("8015237461", "8015237461"),  # depot number: unchanged
        ("12 34", "12 34"),
        ("", ""),
    ],
)
def test_format_iban(text, shown) -> None:
    assert format_iban(text) == shown


@pytest.fixture(autouse=True)
def no_message_boxes(monkeypatch):
    for name in ("information", "warning", "critical"):
        monkeypatch.setattr(QMessageBox, name, lambda *a, **k: QMessageBox.StandardButton.Ok)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.No)


@pytest.fixture
def acc(qtbot, app):
    w = MainWindow(app)
    qtbot.addWidget(w)
    return w.accounts


def iban_of(config_dir, name: str) -> str | None:
    return next(a.iban for a in load_settings(config_dir).app.own_accounts if a.name == name)


def test_editor_shows_the_iban_grouped_without_unsaved_marker(acc) -> None:
    acc.select_account("ING Extra-Konto")
    acc.select_account("ING Girokonto")
    assert acc.iban.text() == "DE12 3456 7890 1234 5678 90"
    acc.select_account("Bargeld")
    assert not acc.has_unsaved_changes()


@pytest.mark.parametrize(
    "typed", [GIRO, "DE12 3456 7890 1234 5678 90", " de1234567890 1234567890 "]
)
def test_typed_or_pasted_iban_is_saved_normalized(acc, qtbot, config_dir, typed) -> None:
    acc.select_account("ING Extra-Konto")
    acc.iban.setFocus()
    acc.iban.clear()
    qtbot.keyClicks(acc.iban, typed)  # emits textEdited like a user
    assert acc.iban.text() == "DE12 3456 7890 1234 5678 90"
    assert acc.has_unsaved_changes()
    acc.save()
    assert iban_of(config_dir, "ING Extra-Konto") == GIRO
    assert iban_of(config_dir, "ING Girokonto") == GIRO  # untouched


def test_depot_number_is_shown_and_saved_as_typed(acc, qtbot, config_dir) -> None:
    acc.select_account("ING Baufinanzierung")
    qtbot.keyClicks(acc.iban, "8015237461")
    assert acc.iban.text() == "8015237461"
    acc.save()
    assert iban_of(config_dir, "ING Baufinanzierung") == "8015237461"


@pytest.mark.parametrize(
    ("text", "cursor", "shown", "after"),
    [
        ("D", 1, "D", 1),  # not yet an IBAN
        ("de1", 3, "de1", 3),
        ("de12", 4, "DE12", 4),
        ("DE123", 5, "DE12 3", 6),  # the space is put in front of the new character
        ("DE12 34567", 10, "DE12 3456 7", 11),
        ("DE12 3X456", 7, "DE12 3X45 6", 7),  # typed in the middle: cursor after the X
        ("DE12 ", 5, "DE12", 4),  # a typed space at a group end
        ("1234567", 7, "1234567", 7),  # account number: as typed
    ],
)
def test_format_iban_input(text, cursor, shown, after) -> None:
    assert format_iban_input(text, cursor) == (shown, after)


def test_iban_is_grouped_while_typing(acc, qtbot) -> None:
    acc.select_account("ING Extra-Konto")
    acc.iban.setFocus()
    seen = []
    for ch in "de1234567890":
        qtbot.keyClick(acc.iban, ch)
        seen.append(acc.iban.text())
    assert seen[3] == "DE12" and seen[4] == "DE12 3"
    assert acc.iban.text() == "DE12 3456 7890"
    assert acc.iban.cursorPosition() == len(acc.iban.text())
    acc.iban.setCursorPosition(6)  # after "DE12 3"
    qtbot.keyClick(acc.iban, "X")
    assert acc.iban.text() == "DE12 3X45 6789 0"
    assert acc.iban.cursorPosition() == 7


def test_pasted_iban_is_grouped(acc, qtbot) -> None:
    acc.select_account("ING Extra-Konto")
    acc.iban.setFocus()
    QGuiApplication.clipboard().setText(GIRO)
    qtbot.keyClick(acc.iban, Qt.Key.Key_V, Qt.KeyboardModifier.ControlModifier)
    assert acc.iban.text() == "DE12 3456 7890 1234 5678 90"
    assert acc.has_unsaved_changes()
