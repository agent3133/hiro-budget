"""Main window: tabs for transactions, reports and configuration, plus menus."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from PySide6.QtCore import QByteArray, QEvent, QFileSystemWatcher, QPoint, Qt, QTimer, QUrl
from PySide6.QtGui import (
    QContextMenuEvent,
    QDesktopServices,
    QKeySequence,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QFileDialog,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QToolButton,
    QWidget,
)

from hiro_budget import __version__, logfile
from hiro_budget.app import App
from hiro_budget.gui import regex_help, shortcuts
from hiro_budget.gui.accounts_view import AccountsView, saved_balance_list
from hiro_budget.gui.bank_dialog import BankAccessDialog
from hiro_budget.gui.bank_fetch import FetchControl
from hiro_budget.gui.budget_view import BudgetView
from hiro_budget.gui.common import (
    category_choices,
    learning_text,
    money,
    show_error,
)
from hiro_budget.gui.config_view import ConfigView
from hiro_budget.gui.history import History
from hiro_budget.gui.import_dialog import ImportDialog
from hiro_budget.gui.import_history_dialog import ImportHistoryDialog
from hiro_budget.gui.main_menus import MainMenus
from hiro_budget.gui.navigation import (
    ICON_ACCOUNTS,
    ICON_BACK,
    ICON_BOOKINGS,
    ICON_BUDGET,
    ICON_CATEGORIES,
    ICON_FETCH,
    ICON_FORWARD,
    ICON_IMPORT,
    ICON_MOON,
    ICON_OVERVIEW,
    ICON_REPORTS,
    ICON_SUN,
    NavigationView,
    refresh_glyphs,
    set_glyph,
)
from hiro_budget.gui.overview_view import OverviewView
from hiro_budget.gui.reports import NetWorthReport, TopPayeesReport
from hiro_budget.gui.reports_view import ReportsView
from hiro_budget.gui.search import Hit, SearchBox, best
from hiro_budget.gui.shortcuts_dialog import ShortcutsDialog
from hiro_budget.gui.theme import apply_theme
from hiro_budget.gui.titlebar import BadgeButton, TitleBar, side_button
from hiro_budget.gui.training import trainer
from hiro_budget.gui.transactions_view import TransactionsView
from hiro_budget.services import backup, config_edit, export, import_service
from hiro_budget.services import transactions as txs
from hiro_budget.services.budget import month_first_day, month_last_day
from hiro_budget.services.categorization.pipeline import recategorize_all
from hiro_budget.services.transactions import TransactionRow

PREBUILD_DELAY_MS = 1000  # after the window appeared


class MainWindow(QMainWindow):
    def __init__(self, app: App) -> None:
        super().__init__()
        self.app = app
        self.setWindowTitle(self.tr("Hiro Budget"))
        self._initial_size()

        self._reports_need_refresh = False
        self._budget_needs_refresh = False
        # The pages are made when they are first shown (or asked for: self.budget, ...).
        self._built: dict[str, QWidget] = {}
        self._regex_window = None  # Help > Regex cheat sheet…, built when first opened
        self._guide_window = None  # Help > User guide, likewise
        self.tabs = NavigationView()  # sidebar, Windows 11 style
        for name, title, glyph in (
            ("overview", self.tr("Overview"), ICON_OVERVIEW),
            ("budget", self.tr("Budget"), ICON_BUDGET),
            ("transactions", self.tr("Bookings"), ICON_BOOKINGS),
            ("accounts", self.tr("Accounts"), ICON_ACCOUNTS),
            ("reports", self.tr("Reports"), ICON_REPORTS),
            ("config_view", self.tr("Categories & Rules"), ICON_CATEGORIES),
        ):
            self.tabs.add_lazy_tab(lambda n=name: self._build_page(n), title, glyph)
        self.setCentralWidget(self.tabs)
        self.tabs.set_heading("Hiro Budget")  # the product name, never translated
        self.tabs.set_footer(app.settings.db_path.name, str(app.settings.db_path))
        self.tabs.footerClicked.connect(self._open_data_folder)
        self.tabs.set_accounts(saved_balance_list(app))
        self.tabs.accountClicked.connect(self._show_account)
        self.tabs.currentChanged.connect(self._tab_changed)

        self.title_bar = TitleBar(self, self.tabs.toggle)  # in place of the Windows one
        self.title_bar.install()
        self._main_menus = MainMenus(self)
        self._menus = self._main_menus.menus
        self.statusBar().showMessage(str(app.settings.db_path))
        self.trainer = trainer(app)  # the note while it learns, wherever it was started
        self.trainer.started.connect(self._training)
        self.trainer.finished.connect(self._trained)
        self._restore_state()
        self.tabs.currentWidget()  # makes the page the app opens with
        # The bookings take longest to make (~0.5 s): made in the background once the
        # window is up, so the first click on them is as quick as on the others.
        self._prebuild_timer = QTimer(self, singleShot=True, interval=PREBUILD_DELAY_MS)
        self._prebuild_timer.timeout.connect(self._prebuild)
        self._build_title_bar()  # after the restored page: history starts there

    # -- pages, made when they are needed ------------------------------------------

    PAGES = ("overview", "budget", "transactions", "accounts", "reports", "config_view")

    overview = property(lambda self: self._page("overview"))
    budget = property(lambda self: self._page("budget"))
    transactions = property(lambda self: self._page("transactions"))
    accounts = property(lambda self: self._page("accounts"))
    reports = property(lambda self: self._page("reports"))
    config_view = property(lambda self: self._page("config_view"))

    def _page(self, name: str) -> QWidget:
        """The page, made now if it was not shown yet."""
        return self.tabs.widget(self.PAGES.index(name))

    def _if_built(self, name: str):
        """The page if it was made; else None (it loads fresh data when it is)."""
        return self._built.get(name)

    def _is_current(self, name: str) -> bool:
        return self.tabs.currentIndex() == self.PAGES.index(name)

    def _build_page(self, name: str) -> QWidget:
        app = self.app
        if name == "overview":
            page = OverviewView(app)
            page.show_budget.connect(lambda: self.tabs.setCurrentWidget(self.budget))
            page.show_uncategorized.connect(self._show_uncategorized)
            page.show_review.connect(self._show_review)
            page.show_net_worth.connect(self._show_net_worth)
            page.show_account.connect(self._show_account_bookings)
        elif name == "budget":
            page = BudgetView(app)
            page.show_transactions.connect(self._show_category_month)
            page.show_uncategorized.connect(self._show_uncategorized_month)
            page.assignments_changed.connect(self._overview_dirty)
            self._budget_needs_refresh = False
        elif name == "transactions":
            page = TransactionsView(app)
            page.data_changed.connect(self._reports_dirty)
            page.data_changed.connect(self._budget_dirty)
            page.data_changed.connect(self._reload_balances)
            page.data_changed.connect(self._overview_dirty)
            page.create_rule_requested.connect(self._create_rule)
            page.rules_changed.connect(self._rules_changed)
            page.card.editor.dirty_changed.connect(self._update_unsaved)
        elif name == "accounts":
            page = AccountsView(app)
            page.balances_changed.connect(self.tabs.set_accounts)
            page.changed.connect(self._config_changed)
            page.new_booking_requested.connect(self._new_booking_in)
            page.show_account_bookings.connect(self._show_account_bookings)
            page.dirty_changed.connect(self._update_unsaved)
        elif name == "reports":
            page = ReportsView(app)
            page.show_booking.connect(self._show_booking)
            for report in page.pages:
                if isinstance(report, TopPayeesReport):
                    report.show_payee.connect(self._show_payee)
            self._reports_need_refresh = False
        else:
            page = ConfigView(app)
            page.changed.connect(self._config_changed)
            page.show_bookings.connect(self._show_bookings_subset)
            for editor in (page.categories, page.rules):
                editor.dirty_changed.connect(self._update_unsaved)
        self._built[name] = page
        return page

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if not self._built.get("transactions") and not self._prebuild_timer.isActive():
            self._prebuild_timer.start()

    def _prebuild(self) -> None:
        if self._if_built("transactions") is None:
            self.tabs.widget(self.PAGES.index("transactions"))  # made, not shown

    def _reload_balances(self) -> None:
        """Bookings changed: the balances in the sidebar (and on the accounts page), the
        rule hits on the rules page."""
        if (config_view := self._if_built("config_view")) is not None:
            config_view.rules.bookings_changed()
        accounts = self._if_built("accounts")
        if accounts is not None:  # tells the sidebar; a hidden page draws when shown (#58)
            accounts.reload_balances(redraw=self._is_current("accounts"))
        else:
            self.tabs.set_accounts(saved_balance_list(self.app))

    def _rules_changed(self) -> None:
        config_view = self._if_built("config_view")
        if config_view is not None:
            config_view.rules.load()

    def _set_two_lines(self, on: bool) -> None:
        transactions = self._if_built("transactions")
        if transactions is not None:
            transactions.set_two_lines(on)
        else:
            self.app.ui_state.set("transactions.two_lines", on)

    def _open_data_folder(self) -> None:
        """The folder with the database in Explorer."""
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.app.settings.data_dir)))

    def _open_log_folder(self) -> None:
        """Help > Open log folder: warnings and errors with tracebacks (#79)."""
        folder = logfile.log_dir(self.app.settings)
        folder.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def create_backup(self) -> None:
        try:
            folder = backup.create_backup(self.app.settings, "manual")
        except Exception as e:
            show_error(self, self.tr("Backup"), e)
            return
        QMessageBox.information(
            self,
            self.tr("Backup"),
            self.tr(
                "Database and configuration saved in\n%1\n\nTo restore, close the app and "
                "copy the files back."
            ).replace("%1", str(folder)),
        )

    def restore_backup(self) -> None:
        """File > Restore backup…: choose one; restored after the window closed (the
        database is open until then), then the app starts again (#48)."""
        from hiro_budget.gui.restore_dialog import RestoreDialog

        dialog = RestoreDialog(self.app, self)
        if dialog.exec() and dialog.request is not None:
            QTimer.singleShot(0, lambda: self.restart(dialog.request))

    def _open_backup_folder(self) -> None:
        folder = backup.backup_dir(self.app.settings)
        folder.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def _show_account(self, name: str) -> None:
        self.tabs.setCurrentWidget(self.accounts)
        self.accounts.select_account(name)

    # -- view settings remembered between sessions -------------------------------

    def _initial_size(self) -> None:
        """Most of the screen, centred: comfortable on a laptop, roomy on a 4K monitor."""
        screen = self.screen() or QApplication.primaryScreen()
        if screen is None:
            self.resize(1400, 850)
            return
        available = screen.availableGeometry()
        self.resize(int(available.width() * 0.85), int(available.height() * 0.85))
        frame = self.frameGeometry()
        frame.moveCenter(available.center())
        self.move(frame.topLeft())

    def _restore_state(self) -> None:
        ui = self.app.ui_state
        geometry = ui.get("window.geometry", "")
        if geometry:
            self.restoreGeometry(QByteArray.fromBase64(geometry.encode()))
            # saved on a larger screen (4K monitor, opened on the laptop): shrink to fit
            screen = self.screen() or QApplication.primaryScreen()
            if screen is not None and not self.isMaximized():
                available = screen.availableGeometry()
                if self.width() > available.width() or self.height() > available.height():
                    self.resize(
                        min(self.width(), available.width()),
                        min(self.height(), available.height()),
                    )
                    frame = self.frameGeometry()
                    frame.moveCenter(available.center())
                    self.move(frame.topLeft())
        collapsed = ui.get("navigation.collapsed", None)  # None: by window width
        if collapsed is not None:
            self.tabs.set_collapsed(bool(collapsed))
        self.tabs.collapsedChanged.connect(lambda c: ui.set("navigation.collapsed", c))
        tab = ui.get("window.tab", 0)
        if 0 <= tab < self.tabs.count():
            self.tabs.setCurrentIndex(tab)

    def save_state(self) -> None:
        ui = self.app.ui_state
        ui.set("window.geometry", self.saveGeometry().toBase64().data().decode())
        ui.set("window.tab", self.tabs.currentIndex())
        for name in ("transactions", "budget"):
            if (page := self._if_built(name)) is not None:
                page.save_state()

    # -- menus -----------------------------------------------------------------

    # -- title bar -------------------------------------------------------------

    def _title_button(self, glyph: str, fallback: str, slot) -> QToolButton:
        button = QToolButton()
        button.setObjectName("titleButton")
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        set_glyph(button, glyph, fallback)
        button.clicked.connect(slot)
        return button

    def _build_title_bar(self) -> None:
        """Back / forward next to the sidebar button; unsaved changes, import and
        light / dark before the window buttons."""
        bar = self.title_bar
        self.history = History(self.tabs, self)
        self.back_button = self._title_button(ICON_BACK, "←", self.history.back)
        self.back_button.setToolTip(shortcuts.tip(self.tr("Back"), "back_forward", 0))
        self.forward_button = self._title_button(ICON_FORWARD, "→", self.history.forward)
        self.forward_button.setToolTip(shortcuts.tip(self.tr("Forward"), "back_forward", 1))
        bar.add_left(self.back_button)
        bar.add_left(self.forward_button)
        self.history.changed.connect(self._update_history_buttons)
        self._update_history_buttons()
        for index, step in ((0, -1), (1, 1)):
            shortcuts.bind("back_forward", self, lambda s=step: self._alt_arrow(s), index=index)

        self.search = SearchBox(self._search, self)
        keys = shortcuts.keys_text("search")
        self.search.setPlaceholderText(
            self.tr("Search pages, commands, accounts, bookings…") + f"  ({keys})"
        )
        bar.set_center(self.search)
        shortcuts.bind("search", self, self.focus_search)
        for index, step in ((0, 1), (1, -1)):
            shortcuts.bind("regions", self, lambda s=step: self.focus_region(s), index=index)

        self.unsaved_button = QToolButton()
        self.unsaved_button.setObjectName("titleUnsaved")
        self.unsaved_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.unsaved_button.setText("● " + self.tr("Unsaved"))
        self.unsaved_button.clicked.connect(self.save_all)
        bar.add_right(self.unsaved_button)
        self._update_unsaved()  # the editors tell it when they are made

        self.fetch_control = FetchControl(self.app, self)
        self.fetch_control.fetched.connect(self.refresh_all)
        self.fetch_control.setup_needed.connect(self.bank_access)
        set_glyph(self.fetch_control.button, ICON_FETCH, "⟳")
        for widget in (
            self.fetch_control.status,
            self.fetch_control.cancel_button,
            self.fetch_control.button,
        ):
            bar.add_right(widget)

        self.import_button = BadgeButton()
        self.import_button.setObjectName("titleButton")
        self.import_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.import_button.setText(self.tr("Import"))
        self.import_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        set_glyph(self.import_button, ICON_IMPORT)
        self.import_button.clicked.connect(self.import_statements)
        bar.add_right(self.import_button)
        self.new_statements: list[Path] = []
        # new files in the import folder: checked once they are written
        self._folder_timer = QTimer(self, singleShot=True, interval=800)
        self._folder_timer.timeout.connect(self._check_import_folder)
        self._watcher = QFileSystemWatcher(self)
        folder = self.app.settings.import_dir
        if folder.is_dir():
            self._watcher.addPath(str(folder))
        self._watcher.directoryChanged.connect(self._folder_timer.start)
        self._check_import_folder()

        self.theme_button = self._title_button(ICON_MOON, "◐", self.toggle_theme)
        bar.add_right(self.theme_button)
        self._update_theme_button()

    # -- search (title bar) --------------------------------------------------------

    def focus_region(self, step: int) -> None:
        """F6 / Shift+F6: the sidebar and the page's areas, round (#69)."""
        page = self.tabs.currentWidget()
        regions = [self.tabs.list]
        regions += [
            w
            for w in getattr(page, "focus_regions", lambda: [page])()
            if w.isVisible() and w.isEnabled() and w.focusPolicy() != Qt.FocusPolicy.NoFocus
        ] or [page]
        focus = QApplication.focusWidget()
        current = next(
            (
                i
                for i, w in enumerate(regions)
                if focus is not None and (w is focus or w.isAncestorOf(focus))
            ),
            -1 if step > 0 else 0,
        )
        regions[(current + step) % len(regions)].setFocus(Qt.FocusReason.TabFocusReason)

    def focus_search(self) -> None:
        self.search.setFocus()
        self.search.selectAll()

    def _search(self, query: str) -> list[Hit]:
        """Results for the title bar search, by section."""
        hits: list[Hit] = []
        pages = [(self.tabs.tabText(i), i) for i in range(self.tabs.count())]
        for i in best(pages, query, 6):
            keys = shortcuts.native(shortcuts.sequences("go_to_page")[i])
            hits.append(
                Hit(self.tr("Pages"), self.tabs.tabText(i), keys, lambda i=i: self.show_page(i))
            )
        for label, act in best(self._main_menus.commands(), query, 6):
            detail = (
                "✓"
                if act.isChecked()
                else act.shortcut().toString(QKeySequence.SequenceFormat.NativeText)
            )
            hits.append(Hit(self.tr("Commands"), label, detail, act.trigger))
        balances = (
            page.balance_list()
            if (page := self._if_built("accounts")) is not None
            else saved_balance_list(self.app)
        )
        accounts = [(name, (name, cents)) for name, cents in balances]
        for name, cents in best(accounts, query, 5):
            hits.append(
                Hit(self.tr("Accounts"), name, money(cents), lambda n=name: self._show_account(n))
            )
        categories, group = [], ""
        if (page := self._if_built("transactions")) is not None:
            choices = page.choices
        else:
            with self.app.session() as s:
                choices = category_choices(s)
        for ch in choices:
            if ch.is_group_header:
                group = ch.label
            elif ch.category_id is not None:
                categories.append((f"{group} {ch.label.strip()}", (ch, group)))
        for ch, group in best(categories, query, 6):
            hits.append(
                Hit(
                    self.tr("Categories"),
                    ch.label.strip(),
                    group,
                    lambda c=ch.category_id: self._show_category(c),
                )
            )
        hits.extend(self._search_bookings(query))
        return hits

    def _search_bookings(self, query: str) -> list[Hit]:
        """The newest bookings with the text (payee, purpose, notes), then all of them."""
        if len(query) < 2:
            return []
        f = txs.TxFilter(text=query)
        with self.app.session() as s:
            rows = txs.list_transactions(s, f, limit=8)
            total = txs.count_transactions(s, f) if rows else 0
        section = self.tr("Bookings")
        hits = [
            Hit(
                section,
                f"{r.booking_date:%d.%m.%Y}  {r.payee or r.purpose}",
                money(r.amount_cents),
                lambda i=r.id: self._show_booking(i),
            )
            for r in rows
        ]
        if total > len(rows):
            hits.append(
                Hit(
                    section,
                    self.tr("Show all %1 bookings with “%2”")
                    .replace("%1", str(total))
                    .replace("%2", query),
                    "",
                    lambda q=query: self._show_search(q),
                )
            )
        return hits

    def _show_category(self, category_id: int) -> None:
        """Bookings of a category, over the whole period."""
        self.tabs.setCurrentWidget(self.transactions)
        span = self.transactions._full_range()
        start, end = span if span else (None, None)
        self.transactions.show_selection(category_id, start, end)
        self.transactions.table.setFocus()

    def _show_booking(self, tx_id: int) -> None:
        self.tabs.setCurrentWidget(self.transactions)
        self.transactions.show_booking(tx_id)

    def _show_search(self, text: str) -> None:
        """All bookings with the text, over the whole period."""
        self.tabs.setCurrentWidget(self.transactions)
        span = self.transactions._full_range()
        start, end = span if span else (None, None)
        self.transactions.show_selection(txs.ALL, start, end)
        self.transactions.search.setText(text)
        self.transactions.refresh()
        self.transactions.table.setFocus()

    def _update_history_buttons(self) -> None:
        self.back_button.setEnabled(self.history.can_go_back())
        self.forward_button.setEnabled(self.history.can_go_forward())

    def _alt_arrow(self, step: int) -> None:
        """Back / forward, on every page (the budget's months: Alt+PgUp / PgDn, #66)."""
        if step < 0:
            self.history.back()
        else:
            self.history.forward()

    def _editors(self) -> list[tuple[QWidget, str]]:
        """Pages made so far with changes to save, in the order they are saved
        (categories first: rules and accounts refer to them)."""
        editors = []
        if (config_view := self._if_built("config_view")) is not None:
            editors += [
                (config_view.categories, self.tr("categories")),
                (config_view.rules, self.tr("rules")),
            ]
        if (accounts := self._if_built("accounts")) is not None:
            editors.append((accounts, self.tr("accounts")))
        if (bookings := self._if_built("transactions")) is not None:
            editors.append((bookings.card.editor, self.tr("split")))
        return editors

    @staticmethod
    def _is_dirty(editor) -> bool:
        has = getattr(editor, "has_unsaved_changes", None)
        return has() if has is not None else editor.save_button.isEnabled()

    def _update_unsaved(self) -> None:
        if not hasattr(self, "unsaved_button"):  # an editor made before the title bar
            return
        dirty = [name for e, name in self._editors() if self._is_dirty(e)]
        self.unsaved_button.setVisible(bool(dirty))
        self.unsaved_button.setToolTip(
            shortcuts.tip(
                self.tr("Unsaved changes: %1. Click to save them.").replace("%1", ", ".join(dirty)),
                "save_all",
            )
        )

    def save_all(self) -> None:
        """Save every page with unsaved changes (the marker in the title bar)."""
        for editor, _name in self._editors():
            if self._is_dirty(editor):
                editor.save()

    def _check_import_folder(self) -> None:
        """Dot on the import button while the import folder has statements not
        imported yet."""
        with self.app.session() as s:
            self.new_statements = import_service.new_statement_files(
                s, self.app.settings.import_dir
            )
        n = len(self.new_statements)
        self.import_button.set_badge(n > 0)
        tip = shortcuts.tip(self.tr("Import statements"), "import")
        if n:
            names = "\n".join(p.name for p in self.new_statements[:8])
            more = "\n…" if n > 8 else ""
            tip += "\n\n" + self.tr("New in the import folder:") + "\n" + names + more
        self.import_button.setToolTip(tip)

    def _is_dark(self) -> bool:
        return self.palette().window().color().lightness() < 128

    def toggle_theme(self) -> None:
        """Light <-> dark, keeping the design (Rosé Pine or Windows)."""
        pairs = {
            "rose_pine": "rose_pine_dark",
            "rose_pine_dark": "rose_pine",
            "light": "dark",
            "dark": "light",
        }
        current = self.app.settings.app.theme
        code = pairs.get(current, "light" if self._is_dark() else "dark")
        self._main_menus.theme_actions[code].setChecked(True)
        self.set_theme(code)

    def _update_theme_button(self) -> None:
        dark = self._is_dark()
        set_glyph(self.theme_button, ICON_SUN if dark else ICON_MOON)
        text = self.tr("Switch to light mode") if dark else self.tr("Switch to dark mode")
        self.theme_button.setToolTip(shortcuts.tip(text, "light_dark"))

    def changeEvent(self, event) -> None:
        # "As in Windows" and Windows switched light / dark: the other symbol
        if event.type() == QEvent.Type.ApplicationPaletteChange and hasattr(self, "theme_button"):
            self._update_theme_button()
        super().changeEvent(event)

    # -- actions ---------------------------------------------------------------

    def import_statements(self) -> None:
        """The import dialog, with the new statements of the import folder in it."""
        self._check_import_folder()
        dlg = ImportDialog(self.app, self, paths=self.new_statements)
        dlg.exec()
        if dlg.imported_any:
            self.refresh_all()
        self._check_import_folder()

    def fetch_from_bank(self) -> None:
        self.fetch_control.start()

    def bank_access(self) -> None:
        if self.fetch_control.running:
            return  # the logins are in use
        BankAccessDialog(self.app, self).exec()

    def data_folder_dialog(self) -> None:
        from hiro_budget.gui.data_folder_dialog import DataFolderDialog

        DataFolderDialog(self.app, self).exec()

    def show_status(self, text: str) -> None:
        """A note in the status bar; empty: the database path again."""
        self.statusBar().showMessage(text or str(self.app.settings.db_path))

    def has_unsaved_changes(self) -> bool:
        return any(
            (page := self._if_built(name)) is not None and page.has_unsaved_changes()
            for name in ("config_view", "accounts", "transactions")
        )

    def restart(self, request) -> None:
        """Close the window; ``run_gui`` then carries out ``request`` and starts the app
        again (data folder moved, #29)."""
        self.app.restart = request
        if not self.close():
            self.app.restart = None

    def plugins_dialog(self) -> None:
        from hiro_budget.gui.plugins_dialog import PluginsDialog

        PluginsDialog(self.app, self).exec()

    def import_history(self) -> None:
        dlg = ImportHistoryDialog(self.app, self)
        dlg.exec()
        if dlg.deleted_any:
            self.refresh_all()

    def _export_path(self, default_name: str) -> Path | None:
        excel, csv_filter = self.tr("Excel (*.xlsx)"), self.tr("CSV (*.csv)")
        start = str(self.app.settings.root / f"{default_name}.xlsx")
        path, chosen = QFileDialog.getSaveFileName(
            self, self.tr("Export"), start, f"{excel};;{csv_filter}"
        )
        if not path:
            return None
        p = Path(path)
        if p.suffix.lower() not in (".xlsx", ".csv"):
            p = p.with_suffix(".csv" if chosen == csv_filter else ".xlsx")
        return p

    def _exported(self, paths: list[Path]) -> None:
        QMessageBox.information(
            self,
            self.tr("Export"),
            self.tr("Exported to:") + "\n" + "\n".join(str(p) for p in paths),
        )

    def export_transactions(self) -> None:
        rows = self.transactions.model.rows()
        path = self._export_path(f"buchungen_{date.today():%Y-%m-%d}")
        if path is None:
            return
        try:
            export.export_transactions(path, rows, self.app.settings.app.language)
        except Exception as e:  # e.g. file open in Excel
            show_error(self, self.tr("Export"), e)
            return
        self._exported([path])

    def export_report(self) -> None:
        self.reports.refresh()
        self.reports.ensure_overview()  # the category totals of the export
        start, end, _ = self.reports._filter()
        path = self._export_path(f"bericht_{start:%Y-%m-%d}_{end:%Y-%m-%d}")
        if path is None:
            return
        try:
            paths = export.export_report(
                path, self.reports.months, self.reports._cats, self.app.settings.app.language
            )
        except Exception as e:
            show_error(self, self.tr("Export"), e)
            return
        self._exported(paths)

    def recategorize(self) -> None:
        try:
            with self.app.session() as s:
                stats = recategorize_all(s, self.app.settings)
        except Exception as e:
            show_error(self, self.tr("Categorization"), e)
            return
        self.refresh_all()
        QMessageBox.information(self, self.tr("Categorization"), str(stats))

    def _training(self) -> None:
        self.show_status(learning_text())

    def _trained(self, _clf, error: str) -> None:
        self.show_status("")
        if error:
            show_error(self, self.tr("Classifier"), error)

    def retrain(self) -> None:
        """Trains in the background (#55); the questions come when it is done."""
        self.trainer.start(self._retrained)

    def _retrained(self, clf, error: str) -> None:
        if error:
            return  # shown by _trained
        if clf is None:
            QMessageBox.information(
                self,
                self.tr("Classifier"),
                self.tr("Not enough categorized bookings yet (minimum: %1).").replace(
                    "%1", str(self.app.settings.app.ml.min_samples)
                ),
            )
            return
        answer = QMessageBox.question(
            self,
            self.tr("Classifier"),
            self.tr("Classifier trained on %1 bookings. Re-run categorization now?").replace(
                "%1", str(clf.n_samples)
            ),
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.recategorize()

    def reload_config(self) -> None:
        config_view = self._if_built("config_view")
        if config_view is not None and config_view.has_unsaved_changes():
            answer = QMessageBox.question(
                self,
                self.tr("Reload configuration"),
                self.tr("Discard unsaved changes in the category/rule editor?"),
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        try:
            self.app.reload_config()
        except Exception as e:
            show_error(self, self.tr("Reload configuration"), e)
            return
        if config_view is not None:
            config_view.categories.load()
            config_view.rules.load()
        self.refresh_all()

    def set_language(self, code: str) -> None:
        if code == self.app.settings.app.language:
            return
        config_edit.save_language(self.app.settings, code)
        QMessageBox.information(
            self,
            self.tr("Language"),
            self.tr("The language will change after restarting the application."),
        )

    def set_theme(self, code: str) -> None:
        """Switch light/dark mode now and keep it in config.yaml."""
        try:
            config_edit.save_theme(self.app.settings, code)
        except Exception as e:
            show_error(self, self.tr("Appearance"), e)
            return
        apply_theme(QApplication.instance(), code)
        refresh_glyphs(self)
        self.tabs.restyle()  # the sidebar has its own colours per theme
        self.title_bar.restyle()
        self._update_theme_button()
        self.refresh_all()  # budget colours and charts are drawn for light or dark

    def _show_uncategorized(self) -> None:
        self.tabs.setCurrentWidget(self.transactions)
        self.transactions.show_filter(txs.UNCATEGORIZED)

    def _show_bookings_subset(self, ids: list[int], label: str) -> None:
        """Only the given bookings (e.g. the hits of a rule)."""
        self.tabs.setCurrentWidget(self.transactions)
        self.transactions.show_ids(ids, label)

    def _show_account_bookings(self, account_id: int) -> None:
        self.tabs.setCurrentWidget(self.transactions)
        self.transactions.show_account(account_id)

    def _show_net_worth(self) -> None:
        self.tabs.setCurrentWidget(self.reports)
        page = next(p for p in self.reports.pages if isinstance(p, NetWorthReport))
        self.reports.tabs.setCurrentWidget(page)

    def _show_review(self) -> None:
        self.tabs.setCurrentWidget(self.transactions)
        self.transactions.start_review()

    # -- keyboard ----------------------------------------------------------------

    def show_page(self, index: int) -> None:
        """Switch page (wrapping around) and put the keyboard focus into it."""
        self.tabs.setCurrentIndex(index % self.tabs.count())
        self.tabs.currentWidget().setFocus()

    def find_bookings(self) -> None:
        self.show_page(self.tabs.indexOf(self.transactions))
        self.transactions.search.setFocus()
        self.transactions.search.selectAll()

    def save_page(self) -> None:
        if self._is_current("accounts") and self.accounts.has_unsaved_changes():
            self.accounts.save()
        elif self._is_current("transactions") and self.transactions.has_unsaved_changes():
            self.transactions.card.editor.save()
        elif self._is_current("config_view"):
            for editor in (self.config_view.categories, self.config_view.rules):
                if editor.save_button.isEnabled():
                    editor.save()

    def open_context_menu(self) -> None:
        """Right-click menu of the focused list at its current row (Shift+F10)."""
        widget = QApplication.focusWidget()
        if widget is None:
            return
        if isinstance(widget, QAbstractItemView):
            rect = widget.visualRect(widget.currentIndex())
            pos = rect.center() if rect.isValid() else QPoint(4, 4)
            widget = widget.viewport()
        else:
            pos = widget.rect().center()
        event = QContextMenuEvent(QContextMenuEvent.Reason.Keyboard, pos, widget.mapToGlobal(pos))
        QApplication.sendEvent(widget, event)

    def show_shortcuts(self) -> None:
        """F1: the cheat sheet in a regex field, else the keyboard shortcuts."""
        focus = QApplication.focusWidget()
        if isinstance(focus, QLineEdit) and focus.property(regex_help.REGEX_HELP):
            regex_help.show_popup(focus)
            return
        ShortcutsDialog(self).exec()

    def show_regex_help(self) -> None:
        """Help > Regex cheat sheet…: one window, kept open beside the app."""
        if self._regex_window is None:
            self._regex_window = regex_help.RegexHelpWindow(self)
        self._regex_window.show()
        self._regex_window.raise_()
        self._regex_window.activateWindow()

    def show_guide(self) -> None:
        """Help > User guide: one window in the UI language, kept open beside the app."""
        if self._guide_window is None:
            from hiro_budget.gui.guide import GuideWindow

            self._guide_window = GuideWindow(self.app.settings.app.language, self)
        self._guide_window.show()
        self._guide_window.raise_()
        self._guide_window.activateWindow()

    def _about(self) -> None:
        QMessageBox.about(
            self,
            self.tr("About"),
            self.tr(
                "<b>Hiro Budget</b> %1<br><br>"
                "Imports bank statements, categorizes bookings and reports income "
                "and expenses.<br><br>Database: %2<br>Configuration: %3"
            )
            .replace("%1", __version__)
            .replace("%2", str(self.app.settings.db_path))
            .replace("%3", str(self.app.settings.config_dir)),
        )

    # -- refresh plumbing ------------------------------------------------------

    def refresh_all(self) -> None:
        """Data or configuration changed: the pages made so far show it again (the others
        load it when they are made)."""
        if (page := self._if_built("transactions")) is not None:
            if self._is_current("transactions"):
                page.reload_filters()
            else:  # 0.2-0.65 s for all bookings: only when it is shown again (#56)
                page.stale = True
        if (page := self._if_built("reports")) is not None:
            page.reload()
        self._overview_dirty()
        self._reload_balances()
        if (page := self._if_built("budget")) is not None:
            page.refresh()
        self._budget_needs_refresh = False
        self._reports_need_refresh = False

    def _reports_dirty(self) -> None:
        self._reports_need_refresh = True

    def _overview_dirty(self) -> None:
        """Bookings, budget or configuration changed: the overview computes again, now
        when it is the current page, else when it is shown (#61)."""
        if (page := self._if_built("overview")) is not None:
            if self._is_current("overview"):
                page.refresh()
            else:
                page.stale = True

    def _budget_dirty(self) -> None:
        self._budget_needs_refresh = True

    def _show_category_month(self, category_id: int, month: str) -> None:
        self.tabs.setCurrentWidget(self.transactions)
        self.transactions.show_selection(category_id, month_first_day(month), month_last_day(month))

    def _show_payee(self, name: str, start: date, end: date) -> None:
        """Bookings of a payee in a period (from the reports)."""
        self.tabs.setCurrentWidget(self.transactions)
        self.transactions.show_selection(txs.ALL, start, end)
        self.transactions.search.setText(name)

    def _show_uncategorized_month(self, month: str) -> None:
        self.tabs.setCurrentWidget(self.transactions)
        self.transactions.show_selection(
            txs.UNCATEGORIZED, month_first_day(month), month_last_day(month)
        )

    def _tab_changed(self) -> None:
        if self._is_current("budget") and self._budget_needs_refresh:
            self.budget.refresh()
            self._budget_needs_refresh = False
        if self._is_current("reports") and self._reports_need_refresh:
            self.reports.refresh()
            self._reports_need_refresh = False

    def _config_changed(self) -> None:
        self.refresh_all()

    def _create_rule(self, row: TransactionRow) -> None:
        self.tabs.setCurrentWidget(self.config_view)
        self.config_view.rules.prefill_from(row)

    def _new_booking(self) -> None:
        """Ctrl+N: on the accounts page in the selected account (#68)."""
        if self._is_current("accounts"):
            current = self.accounts._current()
            if current is not None and current.balance is not None:
                self._new_booking_in(current.balance.account_id)
                return
        self.tabs.setCurrentWidget(self.transactions)
        self.transactions.new_booking()

    def _new_booking_in(self, account_id: int) -> None:
        self.tabs.setCurrentWidget(self.transactions)
        self.transactions.new_booking(account_id)

    def nativeEvent(self, event_type, message):
        bar = getattr(self, "title_bar", None)  # messages arrive while it is being made
        handled = bar.native_event(event_type, message) if bar is not None else None
        side = side_button(event_type, message) if handled is None else None
        if side is not None and hasattr(self, "history"):  # the mouse's back / forward
            self.history.back() if side < 0 else self.history.forward()
            return True, 1
        return handled if handled is not None else super().nativeEvent(event_type, message)

    def closeEvent(self, event) -> None:
        if self.has_unsaved_changes():
            answer = QMessageBox.question(
                self,
                self.tr("Unsaved changes"),
                self.tr("There are unsaved changes (%1). Quit anyway?").replace(
                    "%1", ", ".join(n for e, n in self._editors() if self._is_dirty(e))
                ),
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        self.fetch_control.shutdown()
        self.save_state()
        if self.trainer.running:  # let it save the new model
            self.hide()
            self.trainer.wait()
        event.accept()
