"""Dialog for setting or removing a category's budget target."""

from __future__ import annotations

from datetime import date

from PySide6.QtWidgets import (
    QComboBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QLabel,
    QSpinBox,
    QVBoxLayout,
)

from hiro_budget.config import TargetDef
from hiro_budget.gui.common import from_qdate, to_qdate


class TargetDialog(QDialog):
    """``result_target`` is the new target, ``removed`` is True if the user removed it."""

    def __init__(self, category_name: str, target: TargetDef | None, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(self.tr("Target for %1").replace("%1", category_name))
        self.result_target: TargetDef | None = None
        self.removed = False

        self.type = QComboBox()
        self.type.addItem(self.tr("Monthly amount"), "monthly")
        self.type.addItem(self.tr("Amount by date"), "by_date")
        self.type.addItem(self.tr("Repeating (e.g. yearly)"), "repeating")
        self.type.addItem(self.tr("Savings balance"), "balance")
        self.amount = QDoubleSpinBox()
        self.amount.setRange(0.01, 10_000_000)
        self.amount.setDecimals(2)
        self.amount.setSuffix(" €")
        self.amount.setGroupSeparatorShown(True)
        self.date = QDateEdit(calendarPopup=True, displayFormat="dd.MM.yyyy")
        self.every = QSpinBox()
        self.every.setRange(1, 120)
        self.every.setSuffix(self.tr(" months"))
        self.explanation = QLabel()
        self.explanation.setWordWrap(True)

        self.form = QFormLayout()
        self.form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.form.addRow(self.tr("Type"), self.type)
        self.form.addRow(self.tr("Amount"), self.amount)
        self.form.addRow(self.tr("Due date"), self.date)
        self.form.addRow(self.tr("Every"), self.every)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        remove = buttons.addButton(self.tr("Remove target"), QDialogButtonBox.ButtonRole.ResetRole)
        remove.setEnabled(target is not None)
        remove.clicked.connect(self._remove)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(self.form)
        layout.addSpacing(6)
        layout.addWidget(self.explanation)
        layout.addStretch(1)
        layout.addWidget(buttons)
        self.setMinimumWidth(460)  # room for the wrapped explanation

        if target is not None:
            self.type.setCurrentIndex(self.type.findData(target.type))
            self.amount.setValue(target.amount)
            self.every.setValue(target.every_months)
            if target.date:
                self.date.setDate(to_qdate(target.date))
        else:
            self.amount.setValue(100)
            self.every.setValue(12)
        if target is None or target.date is None:
            today = date.today()
            self.date.setDate(to_qdate(date(today.year + 1, today.month, 1)))
        self.type.currentIndexChanged.connect(self._update)
        self._update()

    def _update(self) -> None:
        kind = self.type.currentData()
        self.form.setRowVisible(self.date, kind != "monthly")
        self.form.setRowVisible(self.every, kind == "repeating")
        self.explanation.setText(
            {
                "monthly": self.tr("Assign this amount every month."),
                "by_date": self.tr(
                    "Have this amount available by the due date; the remaining amount is "
                    "spread evenly over the months until then."
                ),
                "repeating": self.tr(
                    "Have this amount available on the due date and again every N months "
                    "(e.g. yearly insurance). Each cycle is spread over its months."
                ),
                "balance": self.tr(
                    "Build up and keep this balance (e.g. emergency fund). With a due date "
                    "it is spread over the months until then; leave the date in the past "
                    "for no deadline."
                ),
            }[kind]
        )
        self.adjustSize()  # the rows shown depend on the type

    def _accept(self) -> None:
        kind = self.type.currentData()
        target_date = from_qdate(self.date.date()) if kind != "monthly" else None
        if kind == "balance" and target_date is not None and target_date < date.today():
            target_date = None  # no deadline
        self.result_target = TargetDef(
            type=kind,
            amount=round(self.amount.value(), 2),
            date=target_date,
            every_months=self.every.value(),
        )
        self.accept()

    def _remove(self) -> None:
        self.removed = True
        self.accept()
