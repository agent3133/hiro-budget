"""Dialog for entering or editing a manual booking (expense, income or transfer)."""

from __future__ import annotations

from datetime import date

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QCompleter,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
)

from hiro_budget.app import App
from hiro_budget.gui.common import (
    Choice,
    category_choices,
    fill_choice_combo,
    from_qdate,
    select_choice,
    to_qdate,
)
from hiro_budget.services import lookups
from hiro_budget.services.transactions import ManualEntry

EXPENSE, INCOME = "expense", "income"


class BookingDialog(QDialog):
    def __init__(
        self,
        app: App,
        parent=None,
        entry: ManualEntry | None = None,
        *,
        is_split: bool = False,
        default_account_id: int | None = None,
    ) -> None:
        super().__init__(parent)
        self.app = app
        self.entry: ManualEntry | None = None
        self.is_split = is_split
        self.setWindowTitle(self.tr("Edit booking") if entry else self.tr("New booking"))
        self.setMinimumWidth(460)

        with app.session() as s:
            accounts = lookups.accounts(s)
            payees = lookups.payee_names(s)
            choices = category_choices(s)

        self.account = QComboBox()
        for a in accounts:
            self.account.addItem(a.name, a.id)
        self.date = QDateEdit(calendarPopup=True, displayFormat="dd.MM.yyyy")
        self.direction = QComboBox()
        self.direction.addItem(self.tr("Expense / money out"), EXPENSE)
        self.direction.addItem(self.tr("Income / money in"), INCOME)
        self.amount = QDoubleSpinBox()
        self.amount.setRange(0.0, 10_000_000.0)
        self.amount.setDecimals(2)
        self.amount.setSuffix(" €")
        self.amount.setGroupSeparatorShown(True)
        self.payee = QLineEdit()
        completer = QCompleter(payees, self)
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        completer.setFilterMode(Qt.MatchFlag.MatchContains)
        self.payee.setCompleter(completer)
        self.purpose = QLineEdit()
        self.category = QComboBox()
        auto = Choice(self.tr("(automatic: rules and suggestions)"))
        fill_choice_combo(self.category, [auto, *choices])
        self.notes = QLineEdit()

        form = QFormLayout()
        form.addRow(self.tr("Account"), self.account)
        form.addRow(self.tr("Date"), self.date)
        form.addRow(self.tr("Type"), self.direction)
        form.addRow(self.tr("Amount"), self.amount)
        form.addRow(self.tr("Payee / payer"), self.payee)
        form.addRow(self.tr("Purpose"), self.purpose)
        form.addRow(self.tr("Category / transfer"), self.category)
        form.addRow(self.tr("Note"), self.notes)

        hint = QLabel(
            self.tr(
                "A transfer to another own account creates the counter-booking there "
                "automatically. Bookings entered on a bank account are replaced by the "
                "matching booking of the next statement import."
            )
        )
        hint.setWordWrap(True)
        self.error = QLabel()
        self.error.setStyleSheet("color: #c0392b")

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(hint)
        layout.addWidget(self.error)
        layout.addWidget(buttons)

        if entry is not None:
            self.account.setCurrentIndex(max(0, self.account.findData(entry.account_id)))
            self.date.setDate(to_qdate(entry.booking_date))
            self.direction.setCurrentIndex(0 if entry.amount_cents < 0 else 1)
            self.amount.setValue(abs(entry.amount_cents) / 100)
            self.payee.setText(entry.payee)
            self.purpose.setText(entry.purpose)
            self.notes.setText(entry.notes or "")
            if entry.category_id is not None or entry.transfer_account_id is not None:
                select_choice(self.category, entry.category_id, entry.transfer_account_id)
            else:
                self.category.setCurrentIndex(0)
        else:
            self.date.setDate(to_qdate(date.today()))
            if default_account_id is not None:
                self.account.setCurrentIndex(max(0, self.account.findData(default_account_id)))
            self.category.setCurrentIndex(0)
        if is_split:
            self.category.setEnabled(False)
            self.category.setToolTip(self.tr("Split booking - change the parts via 'Split…'"))
        self.amount.setFocus()
        self.amount.selectAll()

    def _accept(self) -> None:
        cents = round(self.amount.value() * 100)
        if cents == 0:
            self.error.setText(self.tr("Please enter an amount."))
            return
        if not self.payee.text().strip():
            self.error.setText(self.tr("Please enter a payee or payer."))
            return
        choice: Choice | None = self.category.currentData()
        transfer = choice.transfer_account_id if choice and not self.is_split else None
        if transfer is not None and transfer == self.account.currentData():
            self.error.setText(self.tr("A transfer must go to a different account."))
            return
        sign = -1 if self.direction.currentData() == EXPENSE else 1
        self.entry = ManualEntry(
            account_id=self.account.currentData(),
            booking_date=from_qdate(self.date.date()),
            amount_cents=sign * cents,
            payee=self.payee.text().strip(),
            purpose=self.purpose.text().strip(),
            category_id=choice.category_id if choice and not self.is_split else None,
            transfer_account_id=transfer,
            notes=self.notes.text().strip() or None,
        )
        self.accept()
