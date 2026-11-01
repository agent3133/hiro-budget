"""The selected booking as a card next to the bookings list, as in the design: payee
and amount, the facts, its category (changeable), the note and "create rule"."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
)

from hiro_budget.gui.common import (
    Choice,
    amount_color,
    fill_choice_combo,
    fmt_date,
    money,
    select_choice,
)
from hiro_budget.gui.widgets import DetailCard
from hiro_budget.services.transactions import TransactionRow


class BookingCard(DetailCard):
    #: a category (or transfer) was chosen for the shown booking
    category_chosen = Signal(object)
    #: "create rule" for the shown booking
    rule_requested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(self.tr("Booking"), parent)
        self.setMinimumWidth(280)
        self.amount = QLabel()
        font = self.amount.font()
        font.setPointSize(font.pointSize() + 8)
        font.setBold(True)
        self.amount.setFont(font)
        self.empty = QLabel(self.tr("Select a booking to see its details."))
        self.empty.setObjectName("propName")
        self.empty.setWordWrap(True)

        self.facts = QFormLayout()
        self.facts.setHorizontalSpacing(16)
        self.facts.setVerticalSpacing(6)
        self.values: dict[str, QLabel] = {}
        for key, label in (
            ("date", self.tr("Date")),
            ("value_date", self.tr("Value date")),
            ("account", self.tr("Account")),
            ("type", self.tr("Type")),
            ("counterparty", self.tr("Counterparty")),
            ("purpose", self.tr("Purpose")),
            ("details", self.tr("Payment details")),
            ("reference", self.tr("Mandate / reference")),
            ("splits", self.tr("Parts")),
            ("source", self.tr("Assigned by")),
        ):
            name = QLabel(label)
            name.setObjectName("propName")
            value = QLabel()
            value.setWordWrap(True)
            value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            self.facts.addRow(name, value)
            self.values[key] = value

        self.category = QComboBox()
        self.category.setPlaceholderText(self.tr("Choose a category…"))  # uncategorized
        self.category.activated.connect(self._category_activated)
        self.notes = QLineEdit(placeholderText=self.tr("Note for this booking…"))
        self.notes.setClearButtonEnabled(True)
        self.edit_form = QFormLayout()
        self.edit_form.setHorizontalSpacing(16)
        self.edit_form.addRow(self.tr("Category"), self.category)
        self.edit_form.addRow(self.tr("Note"), self.notes)
        self.rule_button = QPushButton(self.tr("Create rule…"))
        self.rule_button.setToolTip(self.tr("A rule for bookings like this one"))
        self.rule_button.clicked.connect(self.rule_requested)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(self.rule_button)

        body = self.body
        body.addWidget(self.amount)
        body.addSpacing(6)
        body.addWidget(self.empty)
        body.addLayout(self.facts)
        body.addSpacing(10)
        body.addLayout(self.edit_form)
        body.addStretch(1)
        body.addLayout(buttons)
        self.show_rows([])

    def set_choices(self, choices: list[Choice]) -> None:
        fill_choice_combo(self.category, choices)

    def show_rows(self, rows: list[TransactionRow], category: str = "", source: str = "") -> None:
        """One booking: all about it; several: their count and sum; none: a hint."""
        single = rows[0] if len(rows) == 1 else None
        for key in self.values:
            self.facts.setRowVisible(self.values[key], False)
        for w in (self.category, self.notes):
            self.edit_form.setRowVisible(w, single is not None)
        self.rule_button.setVisible(single is not None)
        self.empty.setVisible(single is None)
        if single is None:
            total = sum(r.amount_cents for r in rows)
            self.set_heading(self.tr("%n booking(s) selected", "", len(rows)) if rows else "")
            self.amount.setText(money(total) if rows else "")
            self.amount.setStyleSheet(f"color: {amount_color(total).name()};")
            self.empty.setText(
                self.tr("Select a single booking to see and change its details.")
                if rows
                else self.tr("Select a booking to see its details.")
            )
            return
        r = single
        self.set_heading(r.payee)
        self.amount.setText(money(r.amount_cents))
        self.amount.setStyleSheet(f"color: {amount_color(r.amount_cents).name()};")
        facts = {
            "date": fmt_date(r.booking_date),
            "value_date": fmt_date(r.value_date) if r.value_date != r.booking_date else "",
            "account": r.account_name,
            "type": r.booking_type,
            "counterparty": r.counterparty if r.counterparty != r.payee else "",
            "purpose": r.purpose,
            "details": r.details or "",
            "reference": " · ".join(dict.fromkeys(x for x in (r.mandate_ref, r.reference) if x)),
            "splits": category if r.is_split else "",
            "source": source,
        }
        for key, text in facts.items():
            self.values[key].setText(text or "")
            self.facts.setRowVisible(self.values[key], bool(text))
        # the category: changed right here, unless the booking is split or mirrored
        editable = not r.is_split and not r.is_counterpart
        self.category.setEnabled(editable)
        s = r.splits[0] if len(r.splits) == 1 else None
        select_choice(
            self.category,
            s.category_id if s else None,
            s.transfer_account_id if s and not s.category_id else None,
        )
        if not editable:
            self.category.setToolTip(
                self.tr("Split booking: change its parts with Split…")
                if r.is_split
                else self.tr("Counter-booking: change the original booking")
            )
        else:
            self.category.setToolTip("")
        if not self.notes.hasFocus():
            self.notes.setText(r.notes or "")

    def _category_activated(self, _index: int) -> None:
        choice = self.category.currentData()
        if isinstance(choice, Choice) and not choice.is_group_header:
            self.category_chosen.emit(choice)
