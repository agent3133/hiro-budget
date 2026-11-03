"""Smoke tests for the Qt GUI (headless)."""

from pathlib import Path

import pytest
from PySide6.QtWidgets import QMessageBox

from hiro_budget.gui.import_dialog import ImportDialog
from hiro_budget.gui.main_window import MainWindow
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


def test_hidden_bookings_page_reloads_when_shown(window, app) -> None:
    """#56: refresh_all (after an import, recategorize, config changes) reloads the
    bookings page only when it is the current page; otherwise when it is shown again."""
    view = window.transactions
    window.tabs.setCurrentWidget(window.budget)
    with app.session() as s:
        txs.set_notes(s, view.model.rows()[0].id, "Neu")
    window.refresh_all()
    assert view.stale
    assert all(r.notes != "Neu" for r in view.model.rows())
    window.show()
    window.tabs.setCurrentWidget(view)
    assert not view.stale
    assert any(r.notes == "Neu" for r in view.model.rows())


def test_assign_many_in_one_go(window, app, monkeypatch) -> None:
    """#58: a category for several selected bookings in one session and one refresh; a
    counter-booking among them is left out with one message; the hidden accounts page
    only updates its numbers and the sidebar, and draws when it is shown."""
    from hiro_budget.gui import transactions_view
    from hiro_budget.gui.common import Choice

    view = window.transactions
    accounts = window.accounts  # built, then hidden
    window.tabs.setCurrentWidget(view)
    errors = []
    monkeypatch.setattr(transactions_view, "show_error", lambda *a: errors.append(a[2]))
    changed = []
    view.data_changed.connect(lambda: changed.append(1))
    rows = view.model.rows()
    counter = next(r for r in rows if r.is_counterpart)
    plain = [r for r in rows if not r.is_counterpart and not r.counterparts][:3]
    cat = next(c for c in view.choices if c.category_id is not None)
    view.assign_many([r.id for r in plain] + [counter.id], Choice(cat.label, cat.category_id))
    assert len(changed) == 1
    assert len(errors) == 1 and "counter-booking" in errors[0]
    by_id = {r.id: r for r in view.model.rows()}
    assert all(by_id[r.id].single_category_id == cat.category_id for r in plain)
    assert accounts.stale
    window.show()
    window.tabs.setCurrentWidget(accounts)
    assert not accounts.stale


def test_transfer_arrow_is_in_the_app_font(qtbot) -> None:
    """#56: the transfer arrow is ↔, which Inter and Segoe UI have; ⇄ was in neither, and
    the first one made Windows search all fonts for it (100-390 ms once per process)."""
    from PySide6.QtGui import QFontDatabase, QFontMetrics
    from PySide6.QtWidgets import QApplication

    from hiro_budget.gui import theme

    qapp = QApplication.instance()
    before = theme.active()
    try:
        for name in ("rose_pine", "system"):
            theme.apply_theme(qapp, name)
            font = qapp.font()
            if font.family() not in QFontDatabase.families():
                continue  # not installed here: nothing to check
            assert QFontMetrics(font).inFont("↔"), font.family()
    finally:
        theme.apply_theme(qapp, before)


@pytest.mark.parametrize("language", ["de", "en"])
def test_sidebar_heading_is_the_app_name(qtbot, app, language) -> None:
    from PySide6.QtCore import QCoreApplication
    from PySide6.QtWidgets import QApplication

    from hiro_budget.gui.main import install_translators

    translators = install_translators(QApplication.instance(), language)
    try:
        w = MainWindow(app)
        qtbot.addWidget(w)
        assert w.tabs.heading.text() == "HIRO BUDGET"  # shown in capitals
    finally:
        for t in translators:
            QCoreApplication.removeTranslator(t)


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


def test_split_in_the_card(window) -> None:
    view = window.transactions
    row = next(r for r in view.model.rows() if r.amount_cents == -120_00)
    view.split(row)  # selects it and opens the card's split editor
    editor = view.card.editor
    assert view.card.editing and len(editor.parts) == 2
    assert [p.cents() for p in editor.parts] == [-120_00, 0]
    cats = [c for c in view.choices if c.category_id is not None]
    editor.parts[0].amount.setValue(-100.0)
    editor.parts[1].amount.setValue(-20.0)
    for part, choice in zip(editor.parts, cats, strict=False):
        part.combo.setCurrentIndex(part.combo.findData(choice))
    assert editor.save_button.isEnabled()
    editor.save()
    saved = next(r for r in view.model.rows() if r.id == row.id)
    assert [s.amount_cents for s in saved.splits] == [-100_00, -20_00]
    assert view.card.editing and not editor.has_unsaved_changes()  # shows its parts


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


def test_training_runs_in_the_background(qtbot, app, statement_pdf, monkeypatch):
    """#55: after an import and on Retrain the classifier learns in a worker thread: the
    window stays responsive, the status bar says so meanwhile, a second request while it
    runs trains once more afterwards."""
    import threading

    from hiro_budget.gui import main_window, training
    from hiro_budget.gui.common import learning_text

    window = main_window.MainWindow(app)
    qtbot.addWidget(window)
    release = threading.Event()
    trained = []

    def fit(samples, settings):
        assert release.wait(10)
        trained.append(len(samples))
        return None

    monkeypatch.setattr(training, "fit_and_save", fit)
    dlg = ImportDialog(app, paths=[statement_pdf])
    qtbot.addWidget(dlg)
    dlg._import()  # returns while the training still runs
    trainer = training.trainer(app)
    assert trainer.running
    assert window.statusBar().currentMessage() == learning_text()
    trainer.start()  # e.g. a fetch meanwhile: one more run afterwards
    release.set()
    with qtbot.waitSignal(trainer.finished, timeout=10_000):
        pass
    assert len(trained) == 2
    assert not trainer.running
    assert window.statusBar().currentMessage() == str(app.settings.db_path)

    shown = []
    monkeypatch.setattr(main_window.QMessageBox, "information", lambda *a: shown.append(a[2]))
    window.retrain()  # too few bookings: a message once it is done, no question
    trainer.wait()
    assert len(trained) == 3
    assert shown and "minimum" in shown[0]


def test_report_overview_drawn_when_shown(window, monkeypatch) -> None:
    """#60: with another report tab open, a refresh draws only that one; the overview's
    charts and category totals follow when its tab is shown (or an export needs them)."""
    from hiro_budget.gui import main_window

    reports = window.reports
    window.tabs.setCurrentWidget(reports)
    reports.tabs.setCurrentIndex(2)  # not the overview
    reports._cats = []
    reports.refresh()
    assert reports.months and reports.totals.text()  # the bar above all tabs
    assert reports._overview_stale and not reports._cats
    reports.tabs.setCurrentIndex(0)
    assert not reports._overview_stale and reports._cats

    reports.tabs.setCurrentIndex(2)
    exported = []
    monkeypatch.setattr(window, "_export_path", lambda name: Path("x.xlsx"))
    monkeypatch.setattr(
        main_window.export, "export_report", lambda path, months, cats, lang: exported.append(cats)
    )
    monkeypatch.setattr(window, "_exported", lambda paths: None)
    window.export_report()
    assert exported and exported[0]


def test_overview_computed_again_only_after_changes(window, monkeypatch) -> None:
    """#61: returning to the overview does not compute it again unless bookings, the
    budget or the configuration changed, or the day did."""
    from datetime import date, timedelta

    from hiro_budget.gui import overview_view

    window.show()
    runs = []
    page = window.overview
    original = page.refresh
    monkeypatch.setattr(page, "refresh", lambda: (runs.append(1), original())[1])

    def visit(other) -> None:
        window.tabs.setCurrentWidget(other)
        window.tabs.setCurrentWidget(page)

    window.tabs.setCurrentWidget(page)
    runs.clear()
    visit(window.budget)
    assert runs == []
    window.transactions.data_changed.emit()  # a booking edited
    visit(window.transactions)
    assert runs == [1]
    budget = window.budget
    window.tabs.setCurrentWidget(budget)
    budget.set_assigned(budget.data.envelopes[0].envelope, 12_00)
    window.tabs.setCurrentWidget(page)
    assert runs == [1, 1]
    window.refresh_all()  # e.g. after an import, while the overview is shown
    assert runs == [1, 1, 1]

    class Tomorrow(date):
        @classmethod
        def today(cls):
            return date.today() + timedelta(days=1)

    monkeypatch.setattr(overview_view, "date", Tomorrow)
    visit(window.budget)
    assert runs == [1, 1, 1, 1]


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


def test_import_history_headers_and_file_names_fit(qtbot, window) -> None:
    # #40: "Manuell bearbeitet" was clipped, file names wrapped at "-" and were cut
    from PySide6.QtGui import QFontMetrics

    from hiro_budget.gui.import_history_dialog import ImportHistoryDialog

    dlg = ImportHistoryDialog(window.app)
    qtbot.addWidget(dlg)
    table = dlg.table
    assert not table.wordWrap()
    header = QFontMetrics(table.horizontalHeader().font())
    for col in range(table.columnCount()):
        text = table.horizontalHeaderItem(col).text()
        assert table.columnWidth(col) >= header.horizontalAdvance(text), text
    file_item = table.item(0, 1)
    assert file_item.toolTip() == file_item.text() == dlg.batches[0].source_file


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
    assert card.editing and card.category.isHidden()  # its parts, in the split editor
    assert card.values["splits"].isHidden()
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


def test_rule_hits_search_and_show_bookings(window) -> None:
    # #49: hit count per rule (first match wins), search, the matches on the bookings page
    from hiro_budget.config import RuleDef, RuleMatch
    from hiro_budget.services import config_edit

    with window.app.session() as s:
        samples = config_edit.rule_samples(s)
    broad = RuleDef(id="broad", category="x.y", match=RuleMatch(counterparty="(?i)gmbh"))
    narrow = RuleDef(id="narrow", category="x.y", match=RuleMatch(counterparty="(?i)handwerker"))
    first, second = config_edit.rule_hits(samples, [broad, narrow])
    assert second.matching >= 1 and second.effective == 0 and second.taken == second.matching
    assert first.effective == first.matching
    _, alone = config_edit.rule_hits(samples, [None, narrow])
    assert alone.effective == alone.matching == second.matching

    row = next(r for r in window.transactions.model.rows() if r.payee == "Handwerker GmbH")
    window._create_rule(row)  # a new rule at the top of the list
    editor = window.config_view.rules
    editor._update_match_info()  # after the typing pause
    assert len(editor.hits) == len(editor.drafts)
    h = editor._current_hits()
    assert h.effective == h.matching >= 1
    assert editor.list.currentItem().text().endswith(f"·   {h.effective}")
    assert "href" in editor.match_info.text()

    editor.search.setText(editor._current().id)
    shown = [i for i in range(editor.list.count()) if not editor.list.item(i).isHidden()]
    assert editor.list.currentRow() in shown and len(shown) < editor.list.count()
    editor.search.clear()
    assert not any(editor.list.item(i).isHidden() for i in range(editor.list.count()))

    editor.match_info.linkActivated.emit("#")
    view = window.transactions
    assert window.tabs.currentWidget() is view
    assert {r.id for r in view.model.rows()} == set(h.ids)
    assert view.subset_bar.isVisibleTo(view) and str(len(h.ids)) in view.subset_bar.text()
    view.subset_bar.linkActivated.emit("#")
    assert not view.subset_bar.isVisibleTo(view)
    assert view.model.rowCount() > len(h.ids)


def test_rule_hits_as_matching_each_booking_against_each_rule(window) -> None:
    """#57: the column-wise counting gives the hits of the plain loop (first match wins),
    also with amount bounds, an account pattern and an invalid draft (None)."""
    from hiro_budget.config import RuleDef, RuleMatch
    from hiro_budget.services import config_edit
    from hiro_budget.services.categorization.rules import CompiledRule

    with window.app.session() as s:
        samples = config_edit.rule_samples(s)
    rules = [
        RuleDef(id="a", category="x.y", match=RuleMatch(counterparty="(?i)gmbh")),
        None,
        RuleDef(id="b", category="x.y", match=RuleMatch(amount_max=-10)),
        RuleDef(id="c", category="x.y", match=RuleMatch(account="Giro", purpose="(?i)e")),
        RuleDef(id="d", category="x.y", match=RuleMatch(amount_min=1, amount_max=5000)),
    ]
    expected = [config_edit.RuleHits() for _ in rules]
    compiled = [CompiledRule.compile(r) if r else None for r in rules]
    for tx in samples:
        first = True
        for rule, h in zip(compiled, expected, strict=True):
            if rule is None or not rule.matches(tx):
                continue
            h.matching += 1
            h.ids.append(tx.id)
            h.uncategorized += tx.uncategorized
            h.effective += first
            first = False
    assert config_edit.rule_hits(samples, rules) == expected
    assert all(h.matching for h in expected[2:]) and expected[3].taken


def test_rule_samples_kept_until_bookings_change(window, app) -> None:
    """#57: the rules page keeps the loaded bookings when hidden; an edit of a booking
    loads them again."""
    window.show()
    window.tabs.setCurrentWidget(window.config_view)
    editor = window.config_view.rules
    samples = editor._samples
    assert samples is not None
    window.tabs.setCurrentWidget(window.budget)
    window.tabs.setCurrentWidget(window.config_view)
    assert editor._samples is samples
    window.tabs.setCurrentWidget(window.transactions)
    window.transactions.data_changed.emit()
    assert editor._samples is None
    window.tabs.setCurrentWidget(window.config_view)
    assert editor._samples is not None and editor._samples is not samples


def test_rule_without_hits_is_grey(window) -> None:
    from PySide6.QtCore import Qt

    editor = window.config_view.rules
    editor._add()
    editor.inputs["counterparty"].setText("nothing-matches-this")
    editor._form_changed()
    editor._update_match_info()
    item = editor.list.currentItem()
    assert editor._current_hits().matching == 0 and item.text().endswith("·   0")
    assert item.data(Qt.ItemDataRole.ForegroundRole) is not None
    assert "href" not in editor.match_info.text()
