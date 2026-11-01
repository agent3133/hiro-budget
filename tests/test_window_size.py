"""The window must fit a Full HD laptop at 150 % scaling (about 1280 x 690 usable)."""

import pytest
from PySide6.QtWidgets import QDialog, QMessageBox, QPushButton, QWidget

from hiro_budget.gui.common import fit_to_screen
from hiro_budget.gui.flow_layout import wrapping_row
from hiro_budget.gui.main_window import MainWindow
from hiro_budget.gui.navigation import COMPACT, COMPACT_BELOW, WIDE, icon_font
from hiro_budget.services import import_service

LAPTOP_WIDTH, LAPTOP_HEIGHT = 1280, 690


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
    return w


def test_window_fits_a_laptop(window) -> None:
    from PySide6.QtGui import QFontDatabase

    if "Segoe UI" not in QFontDatabase.families():
        pytest.skip("needs the Windows fonts to measure text as in the application")
    window.tabs.set_compact(True)  # as below COMPACT_BELOW
    minimum = window.minimumSizeHint()
    assert minimum.width() <= LAPTOP_WIDTH - 200, minimum
    assert minimum.height() <= LAPTOP_HEIGHT - 60, minimum  # minus the title bar
    for i in range(window.tabs.count()):
        page = window.tabs.widget(i).minimumSizeHint()
        assert page.width() <= LAPTOP_WIDTH - COMPACT - 60, (window.tabs.tabText(i), page)


def test_sidebar_shows_icons_only_in_narrow_windows(qtbot, window) -> None:
    nav = window.tabs
    window.show()
    window.resize(COMPACT_BELOW + 200, 800)
    qtbot.waitUntil(lambda: not nav.compact)
    assert nav.list.item(0).text() == nav.tabText(0) != ""
    assert nav.list.maximumWidth() == WIDE
    window.resize(COMPACT_BELOW - 200, 800)
    if icon_font() is None:  # no icons to show: keep the labels
        assert not nav.compact
        return
    qtbot.waitUntil(lambda: nav.compact)
    assert nav.list.item(0).text() == ""
    assert nav.list.item(0).toolTip() == nav.tabText(0)  # label as tooltip
    assert nav.list.maximumWidth() == COMPACT


def test_button_rows_wrap(qtbot) -> None:
    buttons = [QPushButton(f"Button number {i}") for i in range(6)]
    row = wrapping_row(buttons)
    qtbot.addWidget(row)
    widest = max(b.sizeHint().width() for b in buttons)
    assert row.minimumSizeHint().width() <= widest + 2  # not all buttons side by side
    one_line = row.layout().heightForWidth(10_000)
    assert row.layout().heightForWidth(widest + 10) > 3 * one_line  # one button per line


def test_dialogs_never_exceed_the_screen(qtbot) -> None:
    dlg = QDialog()
    qtbot.addWidget(dlg)
    available = (dlg.screen() or QWidget().screen()).availableGeometry()
    fit_to_screen(dlg, 50_000, 50_000)
    assert dlg.width() <= available.width() and dlg.height() <= available.height()
    fit_to_screen(dlg, 300, 200)
    assert (dlg.width(), dlg.height()) == (300, 200)
