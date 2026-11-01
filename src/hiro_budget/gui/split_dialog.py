"""Dialog for splitting one booking over several categories / transfers."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QVBoxLayout,
)

from hiro_budget.gui.common import (
    Choice,
    fill_choice_combo,
    fit_to_screen,
    money,
    select_choice,
)
from hiro_budget.services.transactions import SplitInput, TransactionRow


class SplitDialog(QDialog):
    def __init__(self, row: TransactionRow, choices: list[Choice], parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(self.tr("Split booking"))
        fit_to_screen(self, 640, 360)
        self.row = row
        self.choices = choices
        self.parts: list[SplitInput] = []

        info = QLabel(
            f"<b>{row.payee}</b> · {row.booking_date:%d.%m.%Y} · {money(row.amount_cents)}"
        )
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(
            [self.tr("Category / transfer"), self.tr("Amount"), self.tr("Memo")]
        )
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)

        add = QPushButton(self.tr("Add part"))
        remove = QPushButton(self.tr("Remove part"))
        add.clicked.connect(lambda: self._add_row())
        remove.clicked.connect(self._remove_row)
        self.remaining = QLabel()

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.accepted.connect(self._accept)
        self.buttons.rejected.connect(self.reject)

        row_buttons = QHBoxLayout()
        row_buttons.addWidget(add)
        row_buttons.addWidget(remove)
        row_buttons.addStretch(1)
        row_buttons.addWidget(self.remaining)

        layout = QVBoxLayout(self)
        layout.addWidget(info)
        layout.addWidget(self.table, 1)
        layout.addLayout(row_buttons)
        layout.addWidget(self.buttons)

        if row.splits:
            for s in row.splits:
                self._add_row(s.category_id, s.transfer_account_id, s.amount_cents, s.memo or "")
        else:
            self._add_row(amount_cents=row.amount_cents)
        if len(row.splits) <= 1:
            self._add_row(amount_cents=0)
        self._update_remaining()

    def _add_row(
        self,
        category_id: int | None = None,
        transfer_id: int | None = None,
        amount_cents: int | None = None,
        memo: str = "",
    ) -> None:
        # Before inserting: the new row has no spin box yet.
        if amount_cents is None:
            amount_cents = self._remaining_cents()
        i = self.table.rowCount()
        self.table.insertRow(i)
        combo = QComboBox()
        fill_choice_combo(combo, self.choices)
        select_choice(combo, category_id, transfer_id)
        spin = QDoubleSpinBox()
        spin.setRange(-10_000_000, 10_000_000)
        spin.setDecimals(2)
        spin.setSingleStep(1.0)
        spin.setValue(amount_cents / 100)
        spin.valueChanged.connect(self._update_remaining)
        self.table.setCellWidget(i, 0, combo)
        self.table.setCellWidget(i, 1, spin)
        self.table.setCellWidget(i, 2, QLineEdit(memo))
        self._update_remaining()

    def _remove_row(self) -> None:
        r = self.table.currentRow()
        if r >= 0 and self.table.rowCount() > 1:
            self.table.removeRow(r)
            self._update_remaining()

    def _amounts(self) -> list[int]:
        spins = (self.table.cellWidget(i, 1) for i in range(self.table.rowCount()))
        return [round(s.value() * 100) if s else 0 for s in spins]

    def _remaining_cents(self) -> int:
        return self.row.amount_cents - sum(self._amounts())

    def _update_remaining(self) -> None:
        rest = self._remaining_cents()
        self.remaining.setText(self.tr("Remaining: %1").replace("%1", money(rest)))
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(rest == 0)

    def _accept(self) -> None:
        parts = []
        for i, amount in enumerate(self._amounts()):
            if amount == 0:
                continue
            choice: Choice | None = self.table.cellWidget(i, 0).currentData()
            memo = self.table.cellWidget(i, 2).text().strip()
            parts.append(
                SplitInput(
                    amount_cents=amount,
                    category_id=choice.category_id if choice else None,
                    transfer_account_id=choice.transfer_account_id if choice else None,
                    memo=memo or None,
                )
            )
        if not parts:
            return
        self.parts = parts
        self.accept()
