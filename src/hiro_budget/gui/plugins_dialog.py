"""Settings → Plugins: the importers, CSV formats and bank connections the app knows, and
which of the user's own plugins with program code may run (issues #24, #82)."""

from __future__ import annotations

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from hiro_budget.app import App
from hiro_budget.gui.common import fit_to_screen, flat_table, show_error
from hiro_budget.gui.widgets import action_bar

COL_NAME, COL_VERSION, COL_ORIGIN, COL_PROVIDES, COL_STATUS = range(5)
NAME_ROLE = Qt.ItemDataRole.UserRole + 1


class PluginsDialog(QDialog):
    def __init__(self, app: App, parent=None) -> None:
        super().__init__(parent)
        self.app = app
        self.setWindowTitle(self.tr("Plugins"))
        fit_to_screen(self, 900, 520)
        self.enabled: set[str] = set(app.settings.app.plugins.enabled)
        self._loading = False

        note = QLabel(
            self.tr(
                "Importers, CSV formats and bank connections come from plugins. The built-in "
                "ones come with the app; your own are folders in %1. Your plugins with program "
                "code run only when you enable them here."
            ).replace("%1", str(app.settings.plugins_dir))
        )
        note.setWordWrap(True)

        self.table = QTableWidget(0, 5)
        flat_table(self.table)
        self.table.setHorizontalHeaderLabels(
            [
                self.tr("Plugin"),
                self.tr("Version"),
                self.tr("Origin"),
                self.tr("Provides"),
                self.tr("Status"),
            ]
        )
        header = self.table.horizontalHeader()
        for col in (COL_NAME, COL_VERSION, COL_ORIGIN, COL_PROVIDES):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(COL_STATUS, QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.itemChanged.connect(self._item_changed)
        self.table.currentCellChanged.connect(lambda *_: self._show_detail())

        self.detail = QLabel()
        self.detail.setWordWrap(True)
        self.detail.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

        open_folder = QPushButton(self.tr("Open plugin folder"))
        open_folder.clicked.connect(self._open_folder)
        reload = QPushButton(self.tr("Read again"))
        reload.setToolTip(self.tr("Read the plugin folders again, e.g. after adding a plugin"))
        reload.clicked.connect(self._reload)

        box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        box.accepted.connect(self._save)
        box.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(note)
        layout.addWidget(self.table, 1)
        layout.addLayout(action_bar([open_folder, reload]))
        layout.addWidget(self.detail)
        layout.addWidget(box)
        self._fill()

    # -- the list ------------------------------------------------------------------

    def _registry(self):
        from hiro_budget.importers import plugins

        return plugins.discover(self.app.settings.plugins_dir, self.enabled)

    def _provides(self, p) -> str:
        m = p.manifest
        if m is None:
            return ""
        parts = []
        if m.importers:
            parts.append(self.tr("%n importer(s)", "", len(m.importers)))
        if m.formats:
            parts.append(self.tr("%n CSV format(s)", "", len(m.formats)))
        if m.connectors:
            parts.append(self.tr("%n bank connection(s)", "", len(m.connectors)))
        if not parts:
            parts.append(self.tr("library"))
        return ", ".join(parts)

    def _status(self, p) -> str:
        return {
            "available": self.tr("active"),
            "disabled": self.tr("off: contains program code"),
            "replaced": self.tr("replaced by your plugin of this name"),
            "error": self.tr("error: %1").replace("%1", p.problem),
        }[p.status]

    def _fill(self) -> None:
        self.plugins = self._registry().plugins
        self._loading = True
        self.table.setRowCount(0)
        for p in self.plugins:
            row = self.table.rowCount()
            self.table.insertRow(row)
            name = QTableWidgetItem(p.name)
            name.setData(NAME_ROLE, p.name)
            name.setToolTip(p.manifest.description if p.manifest else "")
            if p.origin == "user" and p.manifest is not None and p.has_code:
                name.setFlags(name.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                name.setCheckState(
                    Qt.CheckState.Checked if p.name in self.enabled else Qt.CheckState.Unchecked
                )
                name.setToolTip(self.tr("Enable to let this plugin's program code run"))
            origin = self.tr("built-in") if p.origin == "builtin" else self.tr("own")
            values = [
                p.manifest.version if p.manifest else "",
                origin,
                self._provides(p),
                self._status(p),
            ]
            self.table.setItem(row, COL_NAME, name)
            for col, text in enumerate(values, start=COL_VERSION):
                item = QTableWidgetItem(text)
                item.setToolTip(text)
                self.table.setItem(row, col, item)
        self._loading = False
        if self.plugins:
            self.table.setCurrentCell(0, COL_NAME)
        self._show_detail()

    def _reload(self) -> None:
        from hiro_budget.importers import plugins

        plugins.reset()  # imports read the folders again, too: no restart needed
        self._fill()

    def _show_detail(self) -> None:
        row = self.table.currentRow()
        if not 0 <= row < len(self.plugins):
            self.detail.clear()
            return
        p = self.plugins[row]
        lines = [p.manifest.description] if p.manifest and p.manifest.description else []
        if p.manifest and p.manifest.requires:
            lines.append(self.tr("Requires: %1").replace("%1", ", ".join(p.manifest.requires)))
        lines.append(str(p.path))
        if p.status == "error":  # the status column shows the start of it
            lines.append(p.problem)
        self.detail.setText("\n".join(lines))

    def _item_changed(self, item: QTableWidgetItem) -> None:
        if self._loading or item.column() != COL_NAME:
            return
        name = item.data(NAME_ROLE)
        if item.checkState() == Qt.CheckState.Checked:
            self.enabled.add(name)
        else:
            self.enabled.discard(name)

    def _open_folder(self) -> None:
        folder = self.app.settings.plugins_dir
        folder.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    # -- saving --------------------------------------------------------------------

    def _save(self) -> None:
        new = self.enabled - set(self.app.settings.app.plugins.enabled)
        if new:
            answer = QMessageBox.question(
                self,
                self.tr("Enable plugins"),
                self.tr(
                    "Enable %1? A plugin with program code runs with the rights of the app: "
                    "it can read your bookings and files. Only enable plugins you trust."
                ).replace("%1", ", ".join(sorted(new))),
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        from hiro_budget.importers import plugins
        from hiro_budget.services import config_edit

        try:
            # plugins that are gone stay listed: putting a folder back keeps its choice
            config_edit.save_plugins(self.app.settings, sorted(self.enabled))
        except Exception as e:
            show_error(self, self.tr("Plugins"), e)
            return
        plugins.reset()  # the next import reads the plugins with the new choice
        self.accept()
