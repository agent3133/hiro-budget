"""Smoke tests for the Qt GUI (headless)."""

import pytest
from PySide6.QtWidgets import QDialogButtonBox, QMessageBox

from hiro_budget.gui.import_dialog import ImportDialog
from hiro_budget.gui.main_window import MainWindow
from hiro_budget.gui.split_dialog import SplitDialog
from hiro_budget.services import import_service
from hiro_budget.services import transactions as txs
from tests.fixtures import ing_statement as fx


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
    return w


def test_main_window_shows_bookings(window) -> None:
    view = window.transactions
    assert view.model.rowCount() == fx.BOOKING_COUNT + 1  # + loan counter-booking
    assert "9 " in view.summary.text()


def test_filter_uncategorized_and_assign(window) -> None:
    view = window.transactions
    view.show_filter(txs.UNCATEGORIZED)
    before = view.model.rowCount()
    assert before > 0
    # The mortgage booking is uncategorized too (principal to the loan has no budget
    # category in the test config); pick a plain booking.
    assert any(r.is_split for r in view.model.rows())
    row = next(r for r in view.model.rows() if not r.is_split)
    cat = next(c for c in view.choices if c.category_id is not None)
    view.assign(row.id, cat)
    updated = next(r for r in view.model.rows() if r.id == row.id)
    assert updated.is_manual and not updated.is_uncategorized


def test_split_dialog(qtbot, window) -> None:
    view = window.transactions
    row = next(r for r in view.model.rows() if r.amount_cents == -120_00)
    dlg = SplitDialog(row, view.choices)
    qtbot.addWidget(dlg)
    assert dlg.table.rowCount() == 2
    dlg.table.cellWidget(0, 1).setValue(-100.0)
    dlg.table.cellWidget(1, 1).setValue(-20.0)
    cats = [
        i
        for i in range(dlg.table.cellWidget(0, 0).count())
        if (c := dlg.table.cellWidget(0, 0).itemData(i)) and c.category_id
    ]
    dlg.table.cellWidget(0, 0).setCurrentIndex(cats[0])
    dlg.table.cellWidget(1, 0).setCurrentIndex(cats[1])
    dlg._accept()
    assert [p.amount_cents for p in dlg.parts] == [-100_00, -20_00]


def test_split_dialog_more_than_two_parts(qtbot, window) -> None:
    view = window.transactions
    row = next(r for r in view.model.rows() if r.amount_cents == -120_00)
    cats = [c for c in view.choices if c.category_id is not None]
    dlg = SplitDialog(row, view.choices)
    qtbot.addWidget(dlg)
    dlg.table.cellWidget(0, 1).setValue(-100.0)
    dlg.table.cellWidget(1, 1).setValue(-10.0)
    dlg._add_row()  # pre-filled with the remaining -10.00
    dlg._add_row()  # nothing left: 0.00
    assert dlg._amounts() == [-100_00, -10_00, -10_00, 0]
    dlg.table.setCurrentCell(1, 1)
    dlg._remove_row()
    assert dlg._amounts() == [-100_00, -10_00, 0]
    dlg.table.cellWidget(0, 1).setValue(-90.0)
    dlg.table.cellWidget(2, 1).setValue(-20.0)
    assert dlg.buttons.button(QDialogButtonBox.StandardButton.Ok).isEnabled()
    for i in range(3):
        dlg.table.cellWidget(i, 0).setCurrentIndex(dlg.table.cellWidget(i, 0).findData(cats[i]))
    dlg._accept()
    assert [p.amount_cents for p in dlg.parts] == [-90_00, -10_00, -20_00]

    # Saved and opened again: all three parts, nothing remaining.
    with window.app.session() as s:
        txs.set_splits(s, row.id, dlg.parts)
    view._after_change([row.id], transfer=False)
    saved = next(r for r in view.model.rows() if r.id == row.id)
    assert [s.amount_cents for s in saved.splits] == [-90_00, -10_00, -20_00]
    again = SplitDialog(saved, view.choices)
    qtbot.addWidget(again)
    assert again._amounts() == [-90_00, -10_00, -20_00]
    assert again._remaining_cents() == 0


def test_reports_render(window) -> None:
    window.tabs.setCurrentWidget(window.reports)
    window.reports.refresh()
    assert window.reports._cats and window.reports._slices
    assert "4.000,00" in window.reports.totals.text() or "4,000.00" in window.reports.totals.text()


def test_create_rule_from_booking(window) -> None:
    row = next(r for r in window.transactions.model.rows() if r.payee == "Handwerker GmbH")
    window._create_rule(row)
    editor = window.config_view.rules
    draft = editor._current()
    assert draft.counterparty == r"(?i)Handwerker\ GmbH"
    assert editor.save_button.isEnabled()
    editor._update_match_info()
    assert "1" in editor.match_info.text()


def test_category_editor_add_and_save(window, config_dir) -> None:
    editor = window.config_view.categories
    editor.tree.setCurrentItem(editor.tree.topLevelItem(0))
    editor._add_category()
    editor.save()
    assert "new_category" in (config_dir / "categories.yaml").read_text(encoding="utf-8")


def test_import_dialog(qtbot, app, statement_pdf) -> None:
    dlg = ImportDialog(app, paths=[statement_pdf])
    qtbot.addWidget(dlg)
    assert dlg.files.topLevelItemCount() == 1
    assert dlg.import_button.isEnabled()
    assert dlg.preview.rowCount() == fx.BOOKING_COUNT
    dlg._import()
    assert dlg.imported_any
    with app.session() as s:
        assert len(txs.list_transactions(s)) == fx.BOOKING_COUNT + 1  # + loan counter-booking


def test_learning_hint_while_the_classifier_trains(qtbot, app, statement_pdf, monkeypatch):
    """Training takes a moment after an import and on Retrain: a note says so meanwhile."""
    from hiro_budget.gui import import_dialog, main_window
    from hiro_budget.gui.common import learning_text

    seen = []
    dlg = ImportDialog(app, paths=[statement_pdf])
    qtbot.addWidget(dlg)
    monkeypatch.setattr(
        import_dialog,
        "train_classifier",
        lambda *a: seen.append((dlg.progress.text(), dlg.progress.isVisibleTo(dlg))),
    )
    dlg._import()
    assert seen == [(learning_text(), True)]
    assert dlg.progress.isHidden()

    window = main_window.MainWindow(app)
    qtbot.addWidget(window)
    monkeypatch.setattr(
        main_window,
        "train_classifier",
        lambda *a: seen.append(window.statusBar().currentMessage()),
    )
    window.retrain()  # too few bookings: a message box (patched), no question
    assert seen[-1] == learning_text()
    assert window.statusBar().currentMessage() == str(app.settings.db_path)


def test_notes_editor(window) -> None:
    view = window.transactions
    view.table.selectRow(0)
    assert view.notes.isEnabled()
    view.notes.setText("Testnotiz")
    view._save_note()
    row = view.model.row_at(view.proxy.mapToSource(view.proxy.index(0, 0)).row())
    assert row.notes == "Testnotiz"
    assert "✎ Testnotiz" in view.model.data(view.model.index(view.model.rows().index(row), 4))


def test_import_history_delete(qtbot, monkeypatch, window) -> None:
    from hiro_budget.gui.import_history_dialog import ImportHistoryDialog

    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    dlg = ImportHistoryDialog(window.app)
    qtbot.addWidget(dlg)
    assert dlg.table.rowCount() == 1
    dlg.table.selectRow(0)
    dlg._delete()
    assert dlg.deleted_any and dlg.table.rowCount() == 0


def test_export_menu_actions(window, tmp_path, monkeypatch) -> None:
    from PySide6.QtWidgets import QFileDialog

    target = tmp_path / "x.xlsx"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (str(target), ""))
    window.export_transactions()
    assert target.exists()
    target.unlink()
    window.export_report()
    assert target.exists()


def test_sidebar_navigation(window) -> None:
    nav = window.tabs
    labels = [nav.list.item(i).text() for i in range(nav.list.count())]
    assert len(labels) == nav.count() == 6  # overview first
    assert "&&" not in "".join(labels)  # no tab mnemonics in a list
    changed = []
    nav.currentChanged.connect(changed.append)
    nav.setCurrentWidget(window.reports)
    assert nav.currentWidget() is window.reports
    assert nav.list.currentRow() == nav.currentIndex() == changed[-1]
    nav.list.setCurrentRow(1)  # clicking the sidebar switches the page
    assert nav.currentWidget() is window.budget


def test_switch_light_and_dark_mode(window, config_dir, monkeypatch) -> None:
    from hiro_budget.config import load_settings
    from hiro_budget.gui import main_window

    # The offscreen test platform has no colour schemes: check what is applied.
    applied = []
    monkeypatch.setattr(main_window, "apply_theme", lambda _app, theme: applied.append(theme))
    window.set_theme("dark")
    assert applied == ["dark"]
    assert window.app.settings.app.theme == "dark"
    assert load_settings(config_dir).app.theme == "dark"  # kept in config.yaml
    window.set_theme("system")
    assert applied == ["dark", "system"]
    assert load_settings(config_dir).app.theme == "system"


@pytest.mark.parametrize(
    ("saved", "loaded"), [("obsidian", "rose_pine"), ("obsidian_dark", "rose_pine_dark")]
)
def test_former_theme_names_still_load(saved, loaded) -> None:
    from hiro_budget.config import AppConfig

    assert AppConfig(theme=saved).theme == loaded  # "Rosé Pine" was called "Obsidian"


def test_rose_pine_theme_and_back(window, config_dir) -> None:
    from PySide6.QtWidgets import QApplication

    from hiro_budget.config import load_settings
    from hiro_budget.gui import theme
    from hiro_budget.gui.common import amount_color
    from hiro_budget.gui.navigation import TileDelegate

    qapp = QApplication.instance()
    window.set_theme("rose_pine")
    try:
        assert load_settings(config_dir).app.theme == "rose_pine"
        assert theme.tokens() is theme.ROSE_PINE
        from PySide6.QtGui import QFontInfo

        assert QFontInfo(qapp.font()).family() == "Inter"  # bundled with the app
        assert "{accent}" not in qapp.styleSheet()  # all tokens filled in
        assert theme.ROSE_PINE["accent"].lower() in qapp.styleSheet().lower()
        assert isinstance(window.tabs.list.itemDelegate(), TileDelegate)
        assert amount_color(-1).name().lower() == theme.ROSE_PINE["neg_text"].lower()
        window.tabs.setCurrentWidget(window.reports)
        window.tabs.setCurrentWidget(window.budget)  # pages draw with the theme's colours
        window.tabs.list.grab()
        for page in (window.budget, window.transactions, window.accounts, window.config_view):
            window.tabs.setCurrentWidget(page)  # pills, chips, cards: drawn without errors
            page.grab()
    finally:
        window.set_theme("light")
    assert theme.tokens() is None
    assert qapp.styleSheet() == theme.STYLE  # the native look again
    assert not isinstance(window.tabs.list.itemDelegate(), TileDelegate)


def test_collapse_sidebar(window, monkeypatch) -> None:
    from hiro_budget.gui import navigation

    monkeypatch.setattr(navigation, "icon_font", lambda: "Segoe Fluent Icons")
    nav = window.tabs
    nav.set_collapsed(None)
    nav.narrow = False
    nav._update_compact()
    assert not nav.compact
    nav.toggle.click()  # the button: icons only, remembered
    assert nav.compact and nav.collapsed is True
    assert nav.list.item(0).text() == "" and nav.list.item(0).toolTip()
    assert window.app.ui_state.get("navigation.collapsed") is True
    nav.narrow = False  # a wide window keeps the user's choice
    nav._update_compact()
    assert nav.compact
    nav.toggle_collapsed()  # Ctrl+B: labels back
    assert not nav.compact and nav.list.item(0).text()
    assert window.app.ui_state.get("navigation.collapsed") is False
    nav.set_collapsed(None)  # no choice: by window width
    nav.narrow = True
    nav._update_compact()
    assert nav.compact


def test_menus_in_sidebar(window) -> None:
    nav = window.tabs
    labels = [b.property("label") for b in nav.menu_buttons]
    assert labels == ["File", "View", "Tools", "Help", "Settings"]  # Settings lowest
    assert window.menuWidget() is window.title_bar  # no menu bar
    assert nav.toggle.parent() is window.title_bar
    # shortcuts of the closed menus still reach the window
    keys = {a.shortcut().toString() for a in window.actions()}
    assert {"Ctrl+I", "Ctrl+B", "Ctrl+1"} <= keys


def test_sidebar_accounts_and_footer(window, monkeypatch) -> None:
    from hiro_budget.gui import main_window

    nav = window.tabs
    shown = {
        nav.accounts.topLevelItem(i).text(0): nav.accounts.topLevelItem(i).text(1)
        for i in range(nav.accounts.topLevelItemCount())
    }
    expected = dict(window.accounts.balance_list())
    assert shown.keys() == expected.keys() and shown  # every account with a balance
    name = next(iter(expected))
    item = nav.accounts.topLevelItem(0)
    nav.accounts.itemClicked.emit(item, 0)  # a click opens the account
    assert window.tabs.currentWidget() is window.accounts
    assert window.accounts._current().name == name
    assert nav.footer.text() == window.app.settings.db_path.name
    opened = []
    monkeypatch.setattr(main_window.QDesktopServices, "openUrl", opened.append)
    nav.footer.click()
    assert opened and opened[0].toLocalFile().endswith(window.app.settings.db_path.parent.name)
    nav.set_compact(True)  # icons only: no room for the extras
    assert all(w.isHidden() for w in nav.extras)
    nav.set_compact(False)
    assert not nav.accounts.isHidden()


def test_period_button(window) -> None:
    from datetime import date

    from hiro_budget.gui.common import from_qdate

    view = window.transactions
    period = view.period
    assert "–" in period.text()
    shown = []
    view.date_to.dateChanged.connect(shown.append)
    period.presets[period.tr("Last year")].click()  # both dates, the filter once
    year = date.today().year - 1
    assert from_qdate(view.date_from.date()) == date(year, 1, 1)
    assert from_qdate(view.date_to.date()) == date(year, 12, 31)
    assert len(shown) == 1
    assert str(year) in period.text() and not period.popup.isVisible()
    period.presets[period.tr("Everything")].click()
    first, last = view._full_range()
    assert from_qdate(view.date_from.date()) == first
    view.show_selection(date_from=date(2026, 1, 1), date_to=date(2026, 3, 31))  # by the page
    assert period.text().startswith("01.01.2026") or period.text().startswith("2026-01-01")


def test_booking_card(window) -> None:
    from PySide6.QtCore import QItemSelectionModel

    from hiro_budget.gui.common import Choice, money
    from hiro_budget.gui.models.transaction_table_model import ROW_ROLE

    view = window.transactions
    view.show_filter(txs.UNCATEGORIZED)
    card = view.card
    rows = [view.proxy.index(i, 0).data(ROW_ROLE) for i in range(view.proxy.rowCount())]
    view.table.selectRow(next(i for i, r in enumerate(rows) if r.is_split))
    assert not card.category.isEnabled()  # parts are changed with Split…
    assert not card.values["splits"].isHidden()
    view.table.selectRow(next(i for i, r in enumerate(rows) if not r.is_split))
    row = view.selected_rows()[0]
    assert card.heading.text() == row.payee and card.amount.text() == money(row.amount_cents)
    assert card.values["account"].text() == row.account_name
    assert card.category.isEnabled() and not card.rule_button.isHidden()
    # choosing a category in the card assigns it
    index = next(
        i
        for i in range(card.category.count())
        if isinstance(card.category.itemData(i), Choice)
        and card.category.itemData(i).category_id is not None
    )
    card.category.setCurrentIndex(index)
    card.category.activated.emit(index)
    chosen = card.category.itemData(index).category_id
    with window.app.session() as s:
        assert txs.get_row(s, row.id).single_category_id == chosen
    # several bookings: their count and sum
    view.show_filter(txs.ALL)
    sel = view.table.selectionModel()
    sel.clearSelection()
    for r in (0, 1):
        sel.select(
            view.proxy.index(r, 0),
            QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows,
        )
    assert card.heading.text().startswith("2")
    assert card.rule_button.isHidden() and card.category.isHidden()


def test_rose_pine_dark(window, config_dir) -> None:
    from PySide6.QtWidgets import QApplication

    from hiro_budget.config import load_settings
    from hiro_budget.gui import theme
    from hiro_budget.gui.common import is_dark

    window.set_theme("rose_pine_dark")
    try:
        assert load_settings(config_dir).app.theme == "rose_pine_dark"
        assert theme.tokens() is theme.ROSE_PINE_DARK and is_dark()
        style = QApplication.instance().styleSheet()
        assert "{" + "disabled}" not in style and theme.ROSE_PINE_DARK["accent"] in style
        for page in (window.overview, window.budget, window.transactions, window.reports):
            window.tabs.setCurrentWidget(page)
            page.grab()
    finally:
        window.set_theme("light")
    assert theme.tokens() is None


def test_bookings_sorted_by_the_model(window) -> None:
    """The model sorts (the proxy only passes rows through); the selection stays put."""
    from PySide6.QtCore import Qt

    from hiro_budget.gui.models.transaction_table_model import ROW_ROLE, Col

    view = window.transactions
    view.show_filter(txs.ALL)

    def rows():
        return [view.proxy.index(i, 0).data(ROW_ROLE) for i in range(view.proxy.rowCount())]

    dates = [(r.booking_date, r.id) for r in rows()]
    assert dates == sorted(dates, reverse=True)  # the default: newest first
    view.table.selectRow(2)
    chosen = view.selected_rows()[0].id
    view.table.sortByColumn(Col.AMOUNT, Qt.SortOrder.AscendingOrder)
    amounts = [r.amount_cents for r in rows()]
    assert amounts == sorted(amounts)
    assert view.selected_rows()[0].id == chosen
    view.table.sortByColumn(Col.PAYEE, Qt.SortOrder.DescendingOrder)
    payees = [r.payee.lower() for r in rows()]
    assert payees == sorted(payees, reverse=True)
    view.refresh()  # new rows keep the chosen order
    assert [r.payee.lower() for r in rows()] == payees
