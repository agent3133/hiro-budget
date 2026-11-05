"""File > Restore backup…: the backups with date, reason, size and bookings; the chosen one
is restored after a backup of the current state, then the app restarts (#48)."""

from __future__ import annotations

from PySide6.QtCore import QLocale, Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

from hiro_budget.app import App
from hiro_budget.gui.common import fit_to_screen, show_error
from hiro_budget.gui.widgets import primary
from hiro_budget.services import backup
from hiro_budget.services.data_folder import RestartRequest

RIGHT = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter


class RestoreDialog(QDialog):
    def __init__(self, app: App, parent=None) -> None:
        super().__init__(parent)
        self.app = app
        self.request: RestartRequest | None = None  # set when the user chose one
        self.setWindowTitle(self.tr("Restore backup"))
        hint = QLabel(
            self.tr(
                "Replaces the database and the configuration (categories, rules, accounts) "
                "with the chosen backup. The current state is backed up first, so this can "
                "be undone the same way. The app then restarts."
            )
        )
        hint.setWordWrap(True)
        self.list = QTreeWidget()
        self.list.setRootIsDecorated(False)
        self.list.setHeaderLabels(
            [self.tr("Date"), self.tr("Made"), self.tr("Bookings"), self.tr("Size")]
        )
        self.list.header().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.list.header().setStretchLastSection(False)
        self.list.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        reasons = {
            "import": self.tr("before an import"),
            "manual": self.tr("by hand"),
            "restore": self.tr("before a restore"),
            "cli": self.tr("before a change by the command line"),
            "mcp": self.tr("before a change by an agent"),
        }
        locale = QLocale()
        self.backups = backup.list_backups(app.settings)
        for b in self.backups:
            bookings = backup.backup_bookings(app.settings, b)
            size = backup.backup_size(b)
            item = QTreeWidgetItem(
                [
                    b.created.strftime("%d.%m.%Y %H:%M:%S"),
                    reasons.get(b.reason, b.reason),
                    locale.toString(bookings) if bookings is not None else "–",
                    locale.formattedDataSize(
                        size, 1, QLocale.DataSizeFormat.DataSizeTraditionalFormat
                    ),
                ]
            )
            item.setData(0, Qt.ItemDataRole.UserRole, str(b.path))
            item.setToolTip(0, str(b.path))
            item.setData(3, Qt.ItemDataRole.UserRole, size)
            for col in (2, 3):
                item.setTextAlignment(col, RIGHT)
            item.setDisabled(bookings is None)  # no readable database in it
            self.list.addTopLevelItem(item)
        self.empty = QLabel(self.tr("There are no backups yet."))
        self.empty.setVisible(not self.backups)
        self.restore_button = primary(QPushButton(self.tr("Restore…")))
        self.restore_button.clicked.connect(self._restore)
        text_width = self.restore_button.fontMetrics().horizontalAdvance(self.restore_button.text())
        self.restore_button.setMinimumWidth(text_width + 40)  # the bold label fits
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        buttons.addButton(self.restore_button, QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.rejected.connect(self.reject)
        self.list.currentItemChanged.connect(lambda *_: self._update_button())
        self.list.itemDoubleClicked.connect(lambda *_: self._restore())
        layout = QVBoxLayout(self)
        layout.addWidget(hint)
        layout.addWidget(self.list, 1)
        layout.addWidget(self.empty)
        layout.addWidget(buttons)
        if self.backups:
            self.list.setCurrentItem(self.list.topLevelItem(0))
        self._update_button()
        fit_to_screen(self, 640, 460)

    def _chosen(self) -> backup.Backup | None:
        item = self.list.currentItem()
        if item is None or item.isDisabled():
            return None
        path = item.data(0, Qt.ItemDataRole.UserRole)
        return next((b for b in self.backups if str(b.path) == path), None)

    def _update_button(self) -> None:
        self.restore_button.setEnabled(self._chosen() is not None)

    def _restore(self) -> None:
        b = self._chosen()
        if b is None:
            return
        answer = QMessageBox.question(
            self,
            self.tr("Restore backup"),
            self.tr(
                "Restore the backup of %1? Bookings, budget, categories and rules changed "
                "since then are replaced. The current state is backed up first."
            ).replace("%1", b.created.strftime("%d.%m.%Y %H:%M")),
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:  # the current state first; no pruning, the chosen one may be the oldest
            backup.create_backup(self.app.settings, "restore", prune_old=False)
        except Exception as e:
            show_error(self, self.tr("Restore backup"), e)
            return
        self.request = RestartRequest(delete=[], ui_state=None, restore=b.path)
        self.accept()
