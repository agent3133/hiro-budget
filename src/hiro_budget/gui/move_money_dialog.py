"""Dialog for moving assigned money between budget lines (or Ready to Assign)."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QLabel,
    QVBoxLayout,
)

from hiro_budget.gui.common import error_color, money
from hiro_budget.services.budget import BudgetMonth, Envelope

RTA = "rta"  # combo data for "Ready to Assign"


class MoveMoneyDialog(QDialog):
    """After accept: ``from_id`` / ``to_id`` (Envelope, None = Ready to Assign), ``cents``.

    Categories can be given as plain ids; groups budgeted as a whole are one entry.
    """

    def __init__(
        self,
        month: BudgetMonth,
        parent=None,
        *,
        from_id: int | Envelope | None = None,
        to_id: int | Envelope | None = None,
        cents: int = 0,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(self.tr("Move money"))
        self.setMinimumWidth(420)
        self.from_id: Envelope | None = None
        self.to_id: Envelope | None = None
        self.cents = 0

        self.source = QComboBox()
        self.target = QComboBox()
        for combo in (self.source, self.target):
            combo.addItem(
                self.tr("Ready to Assign (%1)").replace("%1", money(month.ready_to_assign)), RTA
            )
            for g in month.groups:
                if g.budget is not None:
                    text = f"{g.name} ({money(g.budget.available)})"
                    combo.addItem(text, g.budget.envelope)
                    continue
                combo.addItem(g.name, None)
                combo.model().item(combo.count() - 1).setEnabled(False)
                for c in g.categories:
                    combo.addItem(f"    {c.name} ({money(c.available)})", c.envelope)
        for combo, preset in ((self.source, from_id), (self.target, to_id)):
            data = Envelope.of(preset) if preset is not None else RTA
            combo.setCurrentIndex(max(0, self._find(combo, data)))
        self.amount = QDoubleSpinBox()
        self.amount.setRange(0, 10_000_000)
        self.amount.setDecimals(2)
        self.amount.setSuffix(" €")
        self.amount.setGroupSeparatorShown(True)
        self.amount.setValue(max(cents, 0) / 100)
        self.error = QLabel()
        self.error.setStyleSheet(f"color: {error_color().name()}")

        form = QFormLayout()
        form.addRow(self.tr("From"), self.source)
        form.addRow(self.tr("To"), self.target)
        form.addRow(self.tr("Amount"), self.amount)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.error)
        layout.addWidget(buttons)
        self.amount.setFocus()
        self.amount.selectAll()

    @staticmethod
    def _find(combo: QComboBox, data) -> int:
        # findData compares QVariants; Python objects are compared here instead.
        return next((i for i in range(combo.count()) if combo.itemData(i) == data), -1)

    def _accept(self) -> None:
        src, dst = self.source.currentData(), self.target.currentData()
        if src == dst:
            self.error.setText(self.tr("Source and target must differ."))
            return
        if round(self.amount.value() * 100) <= 0:
            self.error.setText(self.tr("Enter an amount."))
            self.amount.setFocus()
            return
        self.from_id = None if src == RTA else src
        self.to_id = None if dst == RTA else dst
        self.cents = round(self.amount.value() * 100)
        self.accept()
