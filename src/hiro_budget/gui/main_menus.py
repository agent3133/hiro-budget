"""The main window's menus, entries at the bottom of the sidebar, and every menu entry as
a command for the search (#78: moved out of ``MainWindow``)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import QObject
from PySide6.QtGui import QAction, QActionGroup
from PySide6.QtWidgets import QMenu

from hiro_budget.gui import shortcuts
from hiro_budget.gui.navigation import ICON_FILE, ICON_HELP, ICON_SETTINGS, ICON_TOOLS, ICON_VIEW

if TYPE_CHECKING:
    from hiro_budget.gui.main_window import MainWindow


class MainMenus(QObject):
    """Builds the menus of ``w`` (actions parented to the window, their slots on it) and
    adds the actions with a shortcut to the window, so they work while a menu is closed."""

    def __init__(self, w: MainWindow) -> None:
        super().__init__(w)
        file_menu = QMenu(self.tr("&File"), w)
        imp = QAction(self.tr("&Import statements…"), w)
        shortcuts.apply(imp, "import")
        imp.triggered.connect(w.import_statements)
        file_menu.addAction(imp)
        fetch = file_menu.addAction(self.tr("Fe&tch from bank"), w.fetch_from_bank)
        shortcuts.apply(fetch, "fetch")
        new_booking = QAction(self.tr("&New booking…"), w)
        shortcuts.apply(new_booking, "new_booking")
        new_booking.triggered.connect(w._new_booking)
        file_menu.addAction(new_booking)
        file_menu.addAction(self.tr("Import &history…"), w.import_history)
        save = QAction(self.tr("&Save"), w)
        shortcuts.apply(save, "save")
        save.setToolTip(self.tr("Save the changes of the current page"))
        save.triggered.connect(w.save_page)
        file_menu.addAction(save)
        save_all = file_menu.addAction(self.tr("Save &all"), w.save_all)
        shortcuts.apply(save_all, "save_all")
        file_menu.addSeparator()
        export_menu = file_menu.addMenu(self.tr("&Export"))
        export = export_menu.addAction(
            self.tr("&Bookings (current filter)…"), w.export_transactions
        )
        shortcuts.apply(export, "export")
        export_menu.addAction(self.tr("&Report (current period)…"), w.export_report)
        file_menu.addSeparator()
        file_menu.addAction(self.tr("Create &backup"), w.create_backup)
        file_menu.addAction(self.tr("Open backup &folder"), w._open_backup_folder)
        file_menu.addAction(self.tr("&Restore backup…"), w.restore_backup)
        file_menu.addSeparator()
        quit_action = QAction(self.tr("&Quit"), w)
        shortcuts.apply(quit_action, "quit")
        quit_action.triggered.connect(w.close)
        file_menu.addAction(quit_action)

        view = QMenu(self.tr("&View"), w)
        self.page_actions: list[QAction] = []  # in the search as pages, not commands
        page_keys = shortcuts.sequences("go_to_page")
        for i in range(w.tabs.count()):
            page = QAction(w.tabs.tabText(i), w)
            page.setShortcut(page_keys[i])
            page.triggered.connect(lambda _=False, i=i: w.show_page(i))
            view.addAction(page)
            self.page_actions.append(page)
        view.addSeparator()
        for label, shortcut_id, step in (
            (self.tr("&Next page"), "next_page", 1),
            (self.tr("&Previous page"), "previous_page", -1),
        ):
            act = QAction(label, w)
            shortcuts.apply(act, shortcut_id)
            act.triggered.connect(lambda _=False, s=step: w.show_page(w.tabs.currentIndex() + s))
            view.addAction(act)
        view.addSeparator()
        two_lines = QAction(self.tr("&Two lines per booking"), w)
        two_lines.setCheckable(True)
        two_lines.setChecked(bool(w.app.ui_state.get("transactions.two_lines", True)))
        two_lines.toggled.connect(w._set_two_lines)
        view.addAction(two_lines)
        sidebar = QAction(self.tr("&Collapse or expand sidebar"), w)
        shortcuts.apply(sidebar, "sidebar")
        sidebar.triggered.connect(w.tabs.toggle_collapsed)
        view.addAction(sidebar)
        light_dark = view.addAction(self.tr("&Light or dark"), w.toggle_theme)
        shortcuts.apply(light_dark, "light_dark")
        find = QAction(self.tr("&Search bookings"), w)
        shortcuts.apply(find, "find")
        find.triggered.connect(w.find_bookings)
        view.addAction(find)
        refresh = QAction(self.tr("&Refresh"), w)
        shortcuts.apply(refresh, "refresh")
        refresh.triggered.connect(w.refresh_all)
        view.addAction(refresh)
        menu_key = QAction(self.tr("Context &menu"), w)
        shortcuts.apply(menu_key, "context_menu")
        menu_key.triggered.connect(w.open_context_menu)
        view.addAction(menu_key)

        tools = QMenu(self.tr("&Tools"), w)
        tools.addAction(self.tr("Re-run &categorization"), w.recategorize)
        tools.addAction(self.tr("Re&train classifier"), w.retrain)
        tools.addAction(self.tr("Show &uncategorized"), w._show_uncategorized)
        tools.addAction(self.tr("Show suggestions to &review"), w._show_review)
        tools.addSeparator()
        tools.addAction(self.tr("&Reload configuration files"), w.reload_config)

        settings = QMenu(self.tr("&Settings"), w)
        settings.addAction(self.tr("&Bank access…"), w.bank_access)
        settings.addAction(self.tr("&Plugins…"), w.plugins_dialog)
        settings.addAction(self.tr("&Data folder…"), w.data_folder_dialog)
        lang_menu = settings.addMenu(self.tr("&Language"))
        group = QActionGroup(w)
        for code, label in (("de", "Deutsch"), ("en", "English")):
            act = QAction(label, w, checkable=True)
            act.setChecked(w.app.settings.app.language == code)
            act.triggered.connect(lambda _=False, c=code: w.set_language(c))
            group.addAction(act)
            lang_menu.addAction(act)
        theme_menu = settings.addMenu(self.tr("&Appearance"))
        themes = QActionGroup(w)
        self.theme_actions: dict[str, QAction] = {}
        for code, label in (
            ("system", self.tr("As in &Windows")),
            ("light", self.tr("&Light")),
            ("dark", self.tr("&Dark")),
            ("rose_pine", self.tr("&Rosé Pine")),
            ("rose_pine_dark", self.tr("Rosé Pine d&ark")),
        ):
            act = QAction(label, w, checkable=True)
            act.setChecked(w.app.settings.app.theme == code)
            act.triggered.connect(lambda _=False, c=code: w.set_theme(c))
            themes.addAction(act)
            self.theme_actions[code] = act
            theme_menu.addAction(act)

        help_menu = QMenu(self.tr("&Help"), w)
        keys = QAction(self.tr("&Keyboard shortcuts"), w)
        shortcuts.apply(keys, "help")
        keys.triggered.connect(w.show_shortcuts)
        guide = help_menu.addAction(self.tr("&User guide"), w.show_guide)
        shortcuts.apply(guide, "guide")
        help_menu.addAction(keys)
        help_menu.addAction(self.tr("&Regex cheat sheet…"), w.show_regex_help)
        help_menu.addAction(self.tr("Open &log folder"), w._open_log_folder)
        help_menu.addAction(self.tr("&About"), w._about)

        # no menu bar: the menus are entries at the bottom of the sidebar, Settings lowest
        for menu, glyph in (
            (file_menu, ICON_FILE),
            (view, ICON_VIEW),
            (tools, ICON_TOOLS),
            (help_menu, ICON_HELP),
            (settings, ICON_SETTINGS),
        ):
            w.tabs.add_menu(menu, glyph)
        # shortcuts of closed menus only work when the window has the actions too
        self.menus = [file_menu, view, tools, help_menu, settings]
        pending = list(self.menus)
        while pending:
            for act in pending.pop().actions():
                if act.menu() is not None:
                    pending.append(act.menu())
                elif not act.shortcut().isEmpty():
                    w.addAction(act)

    def commands(self) -> list[tuple[str, tuple[str, QAction]]]:
        """Every menu entry as "Menu › Entry", the way to it in its text."""
        out: list[tuple[str, tuple[str, QAction]]] = []
        pending = [(menu.title().replace("&", ""), menu) for menu in self.menus]
        while pending:
            path, menu = pending.pop(0)
            for act in menu.actions():
                if act.isSeparator() or act in self.page_actions:
                    continue
                label = f"{path} › {act.text().replace('&', '').rstrip('…').strip()}"
                if act.menu() is not None:
                    pending.append((label, act.menu()))
                elif act.isEnabled():
                    out.append((label, (label, act)))
        return out
