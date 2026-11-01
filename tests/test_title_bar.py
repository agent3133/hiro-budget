"""Title bar: back / forward, unsaved changes, import folder, light / dark."""

import shutil

import pytest
from PySide6.QtWidgets import QMessageBox

from hiro_budget.gui import theme
from hiro_budget.gui.main_window import MainWindow
from hiro_budget.services import import_service
from hiro_budget.services import transactions as txs
from tests.conftest import write_statement_pdf


@pytest.fixture(autouse=True)
def no_message_boxes(monkeypatch):
    for name in ("information", "warning", "critical"):
        monkeypatch.setattr(QMessageBox, name, lambda *a, **k: QMessageBox.StandardButton.Ok)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.No)


@pytest.fixture
def window(qtbot, app, statement_pdf):
    with app.session() as s:
        import_service.import_file(s, app.settings, statement_pdf)
    w = MainWindow(app)
    qtbot.addWidget(w)
    w.show_page(0)
    return w


def test_back_and_forward_restore_the_place(window) -> None:
    h = window.history
    assert not window.back_button.isEnabled()
    window.tabs.setCurrentWidget(window.transactions)
    window.transactions.show_filter(txs.UNCATEGORIZED)
    window.transactions.search.setText("miete")
    window.tabs.setCurrentWidget(window.budget)
    window.budget.go("2026-03")
    window.tabs.setCurrentWidget(window.reports)
    assert window.back_button.isEnabled() and not window.forward_button.isEnabled()

    h.back()
    assert window.tabs.currentWidget() is window.budget
    window.budget.go("2026-05")  # changed after coming back: kept for forward / back
    h.back()
    view = window.transactions
    assert window.tabs.currentWidget() is view
    assert view.current_filter().category == txs.UNCATEGORIZED
    assert view.search.text() == "miete"
    view.search.clear()
    h.forward()
    assert window.tabs.currentWidget() is window.budget
    assert window.budget.month == "2026-05"
    assert window.forward_button.isEnabled()

    window.tabs.setCurrentWidget(window.accounts)  # a new way: no forward any more
    assert not h.can_go_forward()
    h.back()
    assert window.tabs.currentWidget() is window.budget


def test_alt_arrows_change_the_month_on_the_budget_page(window) -> None:
    window.tabs.setCurrentWidget(window.budget)
    window.budget.go("2026-03")
    window._alt_arrow(1)
    assert window.budget.month == "2026-04"
    assert window.tabs.currentWidget() is window.budget
    window.tabs.setCurrentWidget(window.transactions)
    window._alt_arrow(-1)  # elsewhere: back
    assert window.tabs.currentWidget() is window.budget


def test_unsaved_marker(window, monkeypatch) -> None:
    assert window.unsaved_button.isHidden()
    window.accounts._set_dirty(True)
    assert not window.unsaved_button.isHidden()
    assert "accounts" in window.unsaved_button.toolTip()
    saved = []
    monkeypatch.setattr(window.accounts, "save", lambda: saved.append(True))
    window.unsaved_button.click()
    assert saved
    window.accounts._set_dirty(False)
    assert window.unsaved_button.isHidden()


def test_new_statements_in_the_import_folder(app, statement_pdf, tmp_path) -> None:
    folder = app.settings.import_dir
    with app.session() as s:
        assert import_service.new_statement_files(s, folder) == []  # no folder yet
    folder.mkdir()
    shutil.copyfile(statement_pdf, folder / "a.pdf")
    other = write_statement_pdf(folder / "b.pdf", ["another statement"])
    with app.session() as s:
        assert import_service.new_statement_files(s, folder) == [folder / "a.pdf", other]
        import_service.import_file(s, app.settings, statement_pdf)
    with app.session() as s:  # the same content was imported (under another name)
        assert import_service.new_statement_files(s, folder) == [other]


def test_import_button_offers_the_new_statements(window, statement_pdf, monkeypatch) -> None:
    from hiro_budget.gui import main_window

    folder = window.app.settings.import_dir
    folder.mkdir()
    new = write_statement_pdf(folder / "new.pdf", ["a new statement"])
    shutil.copyfile(statement_pdf, folder / "old.pdf")  # imported already
    window._check_import_folder()
    assert window.import_button.badge
    assert "new.pdf" in window.import_button.toolTip()
    assert "old.pdf" not in window.import_button.toolTip()
    opened = []

    class FakeDialog:
        imported_any = False

        def __init__(self, app, parent, paths=None):
            opened.append(paths)

        def exec(self):
            return 0

    monkeypatch.setattr(main_window, "ImportDialog", FakeDialog)
    window.import_button.click()
    assert opened == [[new]]


def test_theme_button_switches_light_and_dark(window) -> None:
    try:
        window.set_theme("obsidian")
        window.theme_button.click()
        assert window.app.settings.app.theme == "obsidian_dark"
        assert window._theme_actions["obsidian_dark"].isChecked()
        assert "light" in window.theme_button.toolTip()
        window.theme_button.click()
        assert window.app.settings.app.theme == "obsidian"
    finally:
        window.set_theme("light")
    assert theme.tokens() is None


def hits_of(window, query):
    return [(h.section, h.text) for h in window._search(query)]


def test_search_finds_pages_commands_accounts_and_categories(window) -> None:
    hits = hits_of(window, "budget")
    assert hits[0] == ("Pages", "Budget")
    assert ("Commands", "File › Import statements") in hits_of(window, "import st")
    assert ("Accounts", "Bargeld") in hits_of(window, "barg")
    assert any(section == "Categories" for section, _ in hits_of(window, "lebensmittel"))
    assert hits_of(window, "zzzz-nothing") == []


def test_search_runs_a_hit(window) -> None:
    hit = next(h for h in window._search("accounts") if h.section == "Pages")
    hit.run()
    assert window.tabs.currentWidget() is window.accounts
    hit = next(h for h in window._search("bargeld") if h.section == "Accounts")
    hit.run()
    assert window.accounts.nav_state() == "Bargeld"


def test_search_bookings(window) -> None:
    row = next(r for r in window.transactions.model.rows() if r.payee)
    word = row.payee.split()[0]
    bookings = [h for h in window._search(word) if h.section == "Bookings"]
    assert bookings
    window.tabs.setCurrentWidget(window.overview)
    target = next(h for h in bookings if row.payee in h.text)
    target.run()
    assert window.tabs.currentWidget() is window.transactions
    assert row.id in {r.id for r in window.transactions.selected_rows()}


def test_search_box_keyboard(qtbot, window) -> None:
    from PySide6.QtCore import Qt

    window.show()
    qtbot.waitExposed(window)
    window.focus_search()
    box = window.search
    qtbot.keyClicks(box, "reports")
    box.update_results()  # without waiting for the typing pause
    assert box.results.isVisible()
    assert box.results.current().data(Qt.ItemDataRole.DisplayRole) == "Reports"
    qtbot.keyClick(box, Qt.Key.Key_Return)
    assert window.tabs.currentWidget() is window.reports
    assert box.text() == "" and not box.results.isVisible()
