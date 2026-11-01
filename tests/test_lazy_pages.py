"""Pages are made when they are first shown; until then the window gets along without."""

import pytest
from PySide6.QtWidgets import QMessageBox

from hiro_budget.gui.main_window import MainWindow
from hiro_budget.services import import_service


@pytest.fixture(autouse=True)
def no_message_boxes(monkeypatch):
    for name in ("information", "warning", "critical"):
        monkeypatch.setattr(QMessageBox, name, lambda *a, **k: QMessageBox.StandardButton.Ok)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)


@pytest.fixture
def window(qtbot, app, statement_pdf):
    with app.session() as s:
        import_service.import_file(s, app.settings, statement_pdf)
    app.ui_state.set("window.tab", 1)  # opens with the budget
    w = MainWindow(app)
    qtbot.addWidget(w)
    return w


def test_only_the_start_page_is_made(window) -> None:
    assert set(window._built) == {"budget"}
    assert window.tabs.currentWidget() is window.budget
    assert not window.tabs.is_built(window.PAGES.index("transactions"))


def test_a_page_is_made_when_shown(window) -> None:
    window.show_page(window.PAGES.index("reports"))
    assert "reports" in window._built
    assert window.tabs.currentWidget() is window.reports
    assert window.reports.isVisibleTo(window)
    assert window.tabs.indexOf(window.reports) == window.PAGES.index("reports")


def test_sidebar_balances_without_the_accounts_page(window) -> None:
    assert window.tabs.accounts.topLevelItemCount() > 0
    assert "accounts" not in window._built
    names = [
        window.tabs.accounts.topLevelItem(i).text(0)
        for i in range(window.tabs.accounts.topLevelItemCount())
    ]
    assert names == [name for name, _ in window.accounts.balance_list()]


def test_refresh_and_search_leave_the_other_pages_alone(window) -> None:
    window.refresh_all()
    window._update_unsaved()
    hits = window._search("giro")
    assert hits
    window.save_state()
    assert set(window._built) == {"budget"}


def test_two_lines_before_the_bookings_page(window) -> None:
    window._set_two_lines(False)
    assert "transactions" not in window._built
    assert window.transactions.two_lines is False


def test_unsaved_marker_follows_a_page_made_later(window) -> None:
    window.tabs.setCurrentWidget(window.config_view)
    window.config_view.categories.dirty_changed.emit(True)
    window.config_view.categories.save_button.setEnabled(True)
    window._update_unsaved()
    assert not window.unsaved_button.isHidden()


def test_bookings_made_in_the_background_after_showing(qtbot, window, monkeypatch) -> None:
    window._prebuild_timer.setInterval(10)
    window.show()
    qtbot.waitUntil(lambda: "transactions" in window._built, timeout=3000)
    assert window.tabs.currentWidget() is window.budget  # still on its page
    assert not window.transactions.isVisibleTo(window)
