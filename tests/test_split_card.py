"""Splitting a booking in the detail card of the bookings page (#28)."""

import pytest
from PySide6.QtCore import QCoreApplication, Qt
from PySide6.QtWidgets import QApplication, QMessageBox

from hiro_budget.gui.main_window import MainWindow
from hiro_budget.services import import_service

RENT = -120_00  # a booking of the test statement


@pytest.fixture
def answers(monkeypatch):
    """Message boxes answered without a dialog; ``answers.question`` is what "question"
    returns (Discard / Save / Cancel for an unsaved split)."""

    class Answers:
        question = QMessageBox.StandardButton.No
        asked: list[str] = []

    for name in ("information", "warning", "critical"):
        monkeypatch.setattr(QMessageBox, name, lambda *a, **k: QMessageBox.StandardButton.Ok)

    def question(parent, title, text, *a, **k):
        Answers.asked.append(text)
        return Answers.question

    monkeypatch.setattr(QMessageBox, "question", question)
    Answers.asked = []
    return Answers


@pytest.fixture
def window(qtbot, app, statement_pdf, answers):
    with app.session() as s:
        import_service.import_file(s, app.settings, statement_pdf)
    w = MainWindow(app)
    qtbot.addWidget(w)
    return w


@pytest.fixture
def view(window):
    return window.transactions


def row_of(view, amount: int):
    return next(r for r in view.model.rows() if r.amount_cents == amount)


def categories(view):
    return [c for c in view.choices if c.category_id is not None]


def fill(editor, amounts, choices) -> None:
    while len(editor.parts) < len(amounts):
        editor.add_part()
    for part, cents, choice in zip(editor.parts, amounts, choices, strict=True):
        part.amount.setValue(cents / 100)
        part.combo.setCurrentIndex(part.combo.findData(choice))


def test_split_button_turns_the_category_into_parts(view) -> None:
    rent = row_of(view, RENT)
    view._select_id(rent.id)
    card = view.card
    assert not card.split_button.isHidden()
    assert not card.editing
    card.split_button.click()
    assert card.editing
    assert [p.cents() for p in card.editor.parts] == [RENT, 0]
    assert not card.editor.has_unsaved_changes()  # nothing changed yet


def test_remaining_blocks_saving(view) -> None:
    view.split(row_of(view, RENT))
    editor = view.card.editor
    editor.parts[0].amount.setValue(-100.0)  # 20.00 not assigned
    assert editor.has_unsaved_changes() and not editor.save_button.isEnabled()
    assert editor.remaining_cents() == -20_00
    editor.save()  # Ctrl+S / the title bar marker: nothing happens
    assert not row_of(view, RENT).is_split
    editor.parts[1].amount.setValue(-20.0)
    assert editor.save_button.isEnabled()


def test_add_part_takes_the_rest_and_remove(view) -> None:
    view.split(row_of(view, RENT))
    editor = view.card.editor
    editor.parts[0].amount.setValue(-100.0)
    editor.parts[1].amount.setValue(-10.0)
    third = editor.add_part()
    assert third.cents() == -10_00 and editor.remaining_cents() == 0
    editor.remove_part(editor.parts[1])
    assert [p.cents() for p in editor.parts] == [-100_00, -10_00]


def test_save_three_parts_and_unsplit_again(view) -> None:
    rent = row_of(view, RENT)
    cats = categories(view)
    view.split(rent)
    editor = view.card.editor
    fill(editor, [-90_00, -10_00, -20_00], cats[:3])
    editor.save()
    saved = next(r for r in view.model.rows() if r.id == rent.id)
    assert [s.amount_cents for s in saved.splits] == [-90_00, -10_00, -20_00]
    assert all(s.source.value == "manual" for s in saved.splits)
    # a split booking shows its parts in the card, editable
    view._select_id(next(r.id for r in view.model.rows() if r.id != rent.id))
    view._select_id(rent.id)
    assert view.card.editing and [p.cents() for p in editor.parts] == [-90_00, -10_00, -20_00]
    # all but one part removed: an ordinary booking again
    editor.remove_part(editor.parts[2])
    editor.remove_part(editor.parts[1])
    editor.parts[0].amount.setValue(-120.0)
    editor.save()
    again = next(r for r in view.model.rows() if r.id == rent.id)
    assert not again.is_split and again.splits[0].category_id == cats[0].category_id
    assert not view.card.editing


def test_transfer_part_creates_the_counter_booking(view) -> None:
    rent = row_of(view, RENT)
    transfer = next(c for c in view.choices if c.transfer_account_id is not None)
    cat = categories(view)[0]
    view.split(rent)
    fill(view.card.editor, [-100_00, -20_00], [cat, transfer])
    view.card.editor.save()
    mirrors = [r for r in view.model.rows() if r.counterpart_of_id == rent.id]
    assert [m.amount_cents for m in mirrors] == [20_00]


def test_revert(view) -> None:
    rent = row_of(view, RENT)
    view.split(rent)
    editor = view.card.editor
    editor.parts[0].amount.setValue(-60.0)
    assert editor.revert_button.isEnabled()
    editor.revert_button.click()
    assert not editor.has_unsaved_changes() and not view.card.editing  # not split: back


def test_unsaved_marker_and_ctrl_s(window, view, qtbot) -> None:
    window.show()
    rent = row_of(view, RENT)
    view.split(rent)
    editor = view.card.editor
    assert not window.unsaved_button.isVisible()
    fill(editor, [-100_00, -20_00], categories(view)[:2])
    assert window.unsaved_button.isVisible()
    assert "split" in window.unsaved_button.toolTip()
    assert window.has_unsaved_changes()
    window.show_page(window.tabs.indexOf(view))
    window.save_page()  # Ctrl+S
    assert row_of_id(view, rent.id).is_split
    assert not window.unsaved_button.isVisible()


def row_of_id(view, tx_id):
    return next(r for r in view.model.rows() if r.id == tx_id)


def test_leaving_the_booking_asks(view, answers) -> None:
    rent = row_of(view, RENT)
    other = next(r for r in view.model.rows() if r.id != rent.id and not r.is_counterpart)
    view.split(rent)
    fill(view.card.editor, [-100_00, -20_00], categories(view)[:2])

    answers.question = QMessageBox.StandardButton.Cancel  # stay
    view._select_id(other.id)
    assert "unsaved" in answers.asked[-1]
    assert view.card.row.id == rent.id and view.card.editor.has_unsaved_changes()
    assert [r.id for r in view.selected_rows()] == [rent.id]

    answers.question = QMessageBox.StandardButton.Save
    view._select_id(other.id)
    assert row_of_id(view, rent.id).is_split
    assert view.card.row.id == other.id and not view.card.editor.has_unsaved_changes()


def test_leaving_and_discarding(view, answers) -> None:
    rent = row_of(view, RENT)
    other = next(r for r in view.model.rows() if r.id != rent.id and not r.is_counterpart)
    view.split(rent)
    view.card.editor.parts[0].amount.setValue(-50.0)
    answers.question = QMessageBox.StandardButton.Discard
    view._select_id(other.id)
    assert view.card.row.id == other.id and not row_of_id(view, rent.id).is_split


def test_refresh_keeps_unsaved_parts(view, answers) -> None:
    rent = row_of(view, RENT)
    view.split(rent)
    view.card.editor.parts[0].amount.setValue(-50.0)
    view.refresh()  # e.g. after an import
    assert answers.asked == []
    assert view.card.editor.has_unsaved_changes()
    assert view.card.editor.parts[0].cents() == -50_00


def test_close_asks_about_the_split(window, view, answers) -> None:
    view.split(row_of(view, RENT))
    view.card.editor.parts[0].amount.setValue(-50.0)
    answers.question = QMessageBox.StandardButton.No
    assert not window.close()
    assert "split" in answers.asked[-1]


def test_ctrl_d_and_counter_bookings(window, view, qtbot) -> None:
    window.show()
    qtbot.waitExposed(window)
    window.activateWindow()
    qtbot.waitUntil(lambda: QApplication.activeWindow() is window)
    window.show_page(window.tabs.indexOf(view))
    rent = row_of(view, RENT)
    view._select_id(rent.id)
    view.table.setFocus()
    qtbot.waitUntil(lambda: view.table.hasFocus())
    qtbot.keyClick(view.table, Qt.Key.Key_D, Qt.KeyboardModifier.ControlModifier)
    qtbot.keyRelease(view.table, Qt.Key.Key_Control)  # else Ctrl stays held for later tests
    assert view.card.editing
    assert QApplication.focusWidget() is view.card.editor.parts[0].combo
    view.card.editor.revert_button.click()
    assert view.table.hasFocus()  # not left on the hidden editor

    mirror = next((r for r in view.model.rows() if r.is_counterpart), None)
    if mirror is not None:
        view._select_id(mirror.id)
        assert not view.card.split_button.isEnabled()
        view.split(mirror)
        assert not view.card.editing


def test_several_selected_no_split(view) -> None:
    view.table.selectAll()
    assert len(view.selected_rows()) > 1
    assert not view.card.split_button.isVisible() and not view.card.editing


def test_german(view) -> None:
    from hiro_budget.gui.main import install_translators
    from hiro_budget.gui.split_editor import SplitEditor

    translators = install_translators(QApplication.instance(), "de")
    try:
        editor = SplitEditor()
        assert editor.add_button.text() == "Teil hinzufügen"
        assert editor.save_button.text() == "Aufteilung speichern"
    finally:
        for t in translators:
            QCoreApplication.removeTranslator(t)
        editor.deleteLater()


def test_category_box_not_as_wide_as_the_longest_name(view) -> None:
    """A long category name made the combo box 231 px wide; the card is often 280 px, and
    the label beside the box got 28 of its 53 px ("Cate", #63)."""
    box = view.card.category
    box.addItem("Wohnen: Instandhaltung & Baumarkt und Garten, Werkzeug")
    assert box.minimumSizeHint().width() < 200
