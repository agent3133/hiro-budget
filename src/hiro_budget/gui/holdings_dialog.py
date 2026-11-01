"""Holdings of a securities account per imported valuation (Depotauszug)."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from hiro_budget.app import App
from hiro_budget.gui.common import fit_to_screen, flat_table, fmt_date, fmt_number, money
from hiro_budget.services import accounts as acc_service

RIGHT = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter


class HoldingsDialog(QDialog):
    def __init__(self, app: App, account_id: int, account_name: str, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(self.tr("Holdings – %1").replace("%1", account_name))
        fit_to_screen(self, 900, 400)
        with app.session() as s:
            self.valuations = acc_service.valuations(s, account_id)

        self.date = QComboBox()
        for v in self.valuations:
            self.date.addItem(f"{fmt_date(v.date)}  –  {money(v.total_cents)}")
        self.date.currentIndexChanged.connect(self._show)

        self.table = QTableWidget(0, 7)
        flat_table(self.table)
        self.table.setHorizontalHeaderLabels(
            [
                self.tr("Security"),
                self.tr("ISIN"),
                self.tr("WKN"),
                self.tr("Quantity"),
                self.tr("Price"),
                self.tr("Value"),
                self.tr("Share"),
            ]
        )
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.source = QLabel()

        top = QHBoxLayout()
        top.addWidget(QLabel(self.tr("Statement of")))
        top.addWidget(self.date, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(self.table, 1)
        layout.addWidget(self.source)
        layout.addWidget(buttons)
        self._show()

    def _show(self) -> None:
        idx = self.date.currentIndex()
        if not 0 <= idx < len(self.valuations):
            self.table.setRowCount(0)
            self.source.setText(self.tr("No securities account statement imported yet."))
            return
        v = self.valuations[idx]
        self.table.setRowCount(len(v.holdings))
        for i, h in enumerate(v.holdings):
            price = ""
            if h.price is not None:
                price = f"{fmt_number(h.price, 4, 2)} {h.price_currency or ''}".strip()
            share = h.value_cents / v.total_cents * 100 if v.total_cents else 0.0
            cells = [
                QTableWidgetItem(h.name),
                QTableWidgetItem(h.isin or ""),
                QTableWidgetItem(h.wkn or ""),
                QTableWidgetItem(f"{fmt_number(h.quantity)} {h.unit}"),
                QTableWidgetItem(price),
                QTableWidgetItem(money(h.value_cents)),
                QTableWidgetItem(f"{fmt_number(share, 1)} %"),
            ]
            for col in (3, 4, 5, 6):
                cells[col].setTextAlignment(RIGHT)
            for col, cell in enumerate(cells):
                self.table.setItem(i, col, cell)
        self.table.resizeColumnsToContents()
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.source.setText(
            self.tr("Total %1 · file %2")
            .replace("%1", money(v.total_cents))
            .replace("%2", v.source_file)
        )
