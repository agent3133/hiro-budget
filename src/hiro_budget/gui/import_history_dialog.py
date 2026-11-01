"""Import history: list imported statement files and delete an import."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
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
from hiro_budget.gui.common import fit_to_screen, flat_table, fmt_date, show_error
from hiro_budget.services import import_service
from hiro_budget.services.import_service import BatchInfo

ID_ROLE = Qt.ItemDataRole.UserRole + 1


class ImportHistoryDialog(QDialog):
    def __init__(self, app: App, parent=None) -> None:
        super().__init__(parent)
        self.app = app
        self.deleted_any = False
        self.batches: list[BatchInfo] = []
        self.setWindowTitle(self.tr("Import history"))
        fit_to_screen(self, 950, 450)

        self.table = QTableWidget(0, 7)
        flat_table(self.table)
        self.table.setHorizontalHeaderLabels(
            [
                self.tr("Imported"),
                self.tr("File"),
                self.tr("Account"),
                self.tr("Period"),
                self.tr("No."),
                self.tr("Bookings"),
                self.tr("Manually edited"),
            ]
        )
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.itemSelectionChanged.connect(self._update_buttons)

        hint = QLabel(
            self.tr(
                "Deleting an import removes all of its bookings including categories and splits. "
                "The file can be imported again afterwards."
            )
        )
        hint.setWordWrap(True)

        self.delete_button = QPushButton(self.tr("Delete import…"))
        self.delete_button.clicked.connect(self._delete)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.addButton(self.delete_button, QDialogButtonBox.ButtonRole.ActionRole)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(self.table, 1)
        layout.addWidget(hint)
        layout.addWidget(buttons)
        self.load()

    def load(self) -> None:
        with self.app.session() as s:
            self.batches = import_service.list_batches(s)
        self.table.setRowCount(len(self.batches))
        for i, b in enumerate(self.batches):
            period = (
                f"{fmt_date(b.period_start)} – {fmt_date(b.period_end)}" if b.period_start else ""
            )
            cells = [
                b.imported_at.strftime("%d.%m.%Y %H:%M"),
                b.source_file,
                b.account_name,
                period,
                b.statement_no or "",
                self.tr("%n position(s)", "", b.holding_count)
                if b.holding_count
                else self.tr("balance only")
                if b.valuation
                else str(b.booking_count),
                str(b.manual_count),
            ]
            for col, text in enumerate(cells):
                item = QTableWidgetItem(text)
                item.setData(ID_ROLE, b.id)
                if col >= 4:
                    item.setTextAlignment(
                        Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
                    )
                self.table.setItem(i, col, item)
        self.table.resizeColumnToContents(0)
        self.table.resizeColumnToContents(3)
        self._update_buttons()

    def _selected(self) -> BatchInfo | None:
        rows = self.table.selectionModel().selectedRows()
        return self.batches[rows[0].row()] if rows else None

    def _update_buttons(self) -> None:
        self.delete_button.setEnabled(self._selected() is not None)

    def _delete(self) -> None:
        b = self._selected()
        if b is None:
            return
        if b.valuation:
            text = self.tr("Delete the statement '%1'?").replace("%1", b.source_file)
        else:
            text = (
                self.tr("Delete the import of '%1' with %2 bookings?")
                .replace("%1", b.source_file)
                .replace("%2", str(b.booking_count))
            )
        if b.manual_count:
            text += "\n\n" + self.tr(
                "%1 of these bookings were categorized or split manually; this work will be lost."
            ).replace("%1", str(b.manual_count))
        answer = QMessageBox.question(
            self,
            self.tr("Delete import"),
            text,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            with self.app.session() as s:
                import_service.delete_batch(s, self.app.settings, b.id)
        except Exception as e:
            show_error(self, self.tr("Delete import"), e)
            return
        self.deleted_any = True
        self.load()
