"""The selected booking as a card next to the bookings list, as in the design: payee
and amount, the facts, its category (changeable) or its parts (split editor, #28), the
note and "create rule"."""

from __future__ import annotations

import html
import re

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QSizePolicy,
)

from hiro_budget.gui.common import (
    Choice,
    amount_color,
    fill_choice_combo,
    fmt_date,
    money,
    select_choice,
)
from hiro_budget.gui.split_editor import SplitEditor
from hiro_budget.gui.widgets import DetailCard, caption
from hiro_budget.services.transactions import PayeeHistory, TransactionRow

ZWSP = "\u200b"  # zero-width space: a place where a long token may wrap
LONG_TOKEN = re.compile(r"\S{25,}")


def breakable(text: str, chunk: int = 12) -> str:
    """Tokens of 25+ characters (ARN numbers, references) get a wrap point every
    ``chunk`` characters; a label wraps only between words."""
    return LONG_TOKEN.sub(
        lambda m: ZWSP.join(m[0][i : i + chunk] for i in range(0, len(m[0]), chunk)), text
    )


class FactLabel(QLabel):
    """A selectable value of the card; long tokens wrap, copying leaves out the wrap
    points."""

    def __init__(self) -> None:
        super().__init__()
        self.setWordWrap(True)
        self.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

    def setText(self, text: str) -> None:  # noqa: N802 (Qt name)
        super().setText(breakable(text))

    def text(self) -> str:
        return super().text().replace(ZWSP, "")

    def _copy(self) -> None:
        selected = self.selectedText() or self.text()
        QApplication.clipboard().setText(selected.replace(ZWSP, ""))

    def keyPressEvent(self, event) -> None:  # noqa: N802 (Qt name)
        if event.matches(QKeySequence.StandardKey.Copy):
            self._copy()
            return
        super().keyPressEvent(event)

    def contextMenuEvent(self, event) -> None:  # noqa: N802 (Qt name)
        menu = QMenu(self)
        menu.addAction(self.tr("Copy"), self._copy)
        menu.exec(event.globalPos())


class BookingCard(DetailCard):
    #: a category (or transfer) was chosen for the shown booking
    category_chosen = Signal(object)
    #: "create rule" for the shown booking
    rule_requested = Signal()
    #: show these bookings (ids) in the list, labelled: all of the payee
    payee_requested = Signal(list, str)

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
            # the label column keeps its width next to long values
            name.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred)
            value = FactLabel()
            self.facts.addRow(name, value)
            self.values[key] = value

        self.category = QComboBox()
        self.category.setPlaceholderText(self.tr("Choose a category…"))  # uncategorized
        # not as wide as the longest category name (231 px): the card is often narrower,
        # and the label beside it was cut to "Cate"; the open list shows the names in full
        self.category.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.category.setMinimumContentsLength(8)
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
        self.split_button = QPushButton(self.tr("Split"))
        self.split_button.setToolTip(self.tr("Split over several categories (Ctrl+D)"))
        self.split_button.clicked.connect(self.start_split)
        # the payee's other bookings (#50): count and average, the last ones, all of them
        self.history_caption = caption(self.tr("At this payee"))
        self.history = QLabel()
        self.history.setWordWrap(True)
        self.history.setTextFormat(Qt.TextFormat.RichText)
        self.history.linkActivated.connect(self._history_link)
        self._history: PayeeHistory | None = None
        self.editor = SplitEditor()
        self.editor.revert_requested.connect(self._revert)
        self.editor.hide()
        self._row: TransactionRow | None = None
        buttons = QHBoxLayout()
        buttons.addWidget(self.split_button)
        buttons.addWidget(self.rule_button)
        buttons.addStretch(1)
        buttons.addWidget(self.editor.revert_button)
        buttons.addWidget(self.editor.save_button)

        body = self.body
        body.addWidget(self.amount)
        body.addSpacing(6)
        body.addWidget(self.empty)
        body.addLayout(self.facts)
        body.addSpacing(10)
        body.addLayout(self.edit_form)
        body.addSpacing(10)
        body.addWidget(self.history_caption)
        body.addWidget(self.history)
        body.addWidget(self.editor, 100)
        body.addStretch(1)
        body.addLayout(buttons)
        self.show_rows([])

    def set_choices(self, choices: list[Choice]) -> None:
        fill_choice_combo(self.category, choices)
        self.editor.set_choices(choices)

    @property
    def row(self) -> TransactionRow | None:
        """The booking shown (one selected)."""
        return self._row

    @property
    def editing(self) -> bool:
        """The split editor is shown (a split booking, or "Split" was pressed)."""
        return not self.editor.isHidden()

    def _set_editing(self, on: bool) -> None:
        self.editor.setVisible(on)
        self.edit_form.setRowVisible(self.category, self._row is not None and not on)
        for b in (self.editor.save_button, self.editor.revert_button):
            b.setVisible(on)
        # while editing only Revert and Save at the bottom (they fit a narrow card)
        self.split_button.setVisible(self._row is not None and not on)
        self.rule_button.setVisible(self._row is not None and not on)
        if on:  # the editor shows the parts
            self.facts.setRowVisible(self.values["splits"], False)
        self.history_caption.setVisible(self._history is not None and not on)
        self.history.setVisible(self._history is not None and not on)

    def start_split(self) -> None:
        """Turn the category row into the parts of the shown booking."""
        r = self._row
        if r is None or r.is_counterpart:
            return
        if not self.editing:
            self.editor.load(r)
            self._set_editing(True)
        self.editor.focus_first()

    def _revert(self) -> None:
        r = self._row
        if r is None:
            return
        self.editor.load(r)
        self._set_editing(r.is_split and not r.is_counterpart)

    def show_rows(self, rows: list[TransactionRow], category: str = "", source: str = "") -> None:
        """One booking: all about it; several: their count and sum; none: a hint."""
        single = rows[0] if len(rows) == 1 else None
        keep = (  # unsaved parts of this booking stay (a refresh of the list)
            single is not None
            and self._row is not None
            and single.id == self._row.id
            and self.editor.has_unsaved_changes()
        )
        self._row = single
        for key in self.values:
            self.facts.setRowVisible(self.values[key], False)
        for w in (self.category, self.notes):
            self.edit_form.setRowVisible(w, single is not None)
        self.rule_button.setVisible(single is not None)
        self.empty.setVisible(single is None)
        if single is None or (self._history is not None and single.id not in self._history.ids):
            self.show_history(None)
        if single is None:
            self._set_editing(False)
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
        self.category.setToolTip(
            self.tr("Counter-booking: change the original booking") if r.is_counterpart else ""
        )
        if not self.notes.hasFocus():
            self.notes.setText(r.notes or "")
        self.split_button.setEnabled(not r.is_counterpart)
        if keep:
            self.editor.row = r
            self._set_editing(True)
        elif r.is_split and not r.is_counterpart:  # its parts, editable right here
            self.editor.load(r)
            self._set_editing(True)
        else:
            self.editor.load(r)
            self._set_editing(False)

    def show_history(self, h: PayeeHistory | None) -> None:
        """The payee's other bookings under the booking (None: hidden)."""
        self._history = h
        shown = h is not None and self.editor.isHidden()
        self.history_caption.setVisible(shown)
        self.history.setVisible(shown)
        if h is None:
            return
        lines = []
        if h.previous_amount is not None:
            lines.append(
                "<b>"
                + self.tr("Amount changed: before %1").replace("%1", money(h.previous_amount))
                + "</b>"
            )
        lines.append(
            self.tr("%n booking(s) in the last 12 months", "", h.year_count)
            + ", "
            + self.tr("average %1").replace("%1", money(h.year_average))
        )
        rows = "".join(
            f"<tr><td>{fmt_date(b.booking_date)}</td>"
            f"<td align='right' style='padding-left: 16px'>{money(b.amount_cents)}</td></tr>"
            for b in h.last
        )
        link = self.tr("Show all %1").replace("%1", str(len(h.ids)))
        self.history.setText(
            "<br>".join(lines) + f"<table>{rows}</table><a href='#'>{html.escape(link)}</a>"
        )

    def _history_link(self, _link: str) -> None:
        if self._history is not None:
            label = self.tr("bookings at “%1”").replace("%1", self._history.name)
            self.payee_requested.emit(list(self._history.ids), label)

    def _category_activated(self, _index: int) -> None:
        choice = self.category.currentData()
        if isinstance(choice, Choice) and not choice.is_group_header:
            self.category_chosen.emit(choice)
