"""Splitting a booking right in its card on the bookings page (#28): one row per part with
category / transfer, amount and memo, "Add part", the amount still to assign; Save and
Revert like the other editors (unsaved marker, Ctrl+S, the question on closing)."""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from hiro_budget.gui import shortcuts
from hiro_budget.gui.common import Choice, fill_choice_combo, money, select_choice, warning_color
from hiro_budget.gui.navigation import ICON_DELETE
from hiro_budget.gui.widgets import icon_button, primary, revert_by_key
from hiro_budget.services.transactions import SplitInput, TransactionRow

PartKey = tuple[int | None, int | None, int, str]  # category, transfer, cents, memo


@dataclass
class _Part:
    frame: QFrame
    combo: QComboBox
    amount: QDoubleSpinBox
    memo: QLineEdit
    remove: QPushButton
    original: tuple[int | None, int | None]  # category and transfer it was loaded with

    def target(self) -> tuple[int | None, int | None]:
        """Category and transfer of the part: what it was loaded with until another entry
        is picked (a loan payment's category plus transfer, or a transfer to an account
        the picker does not offer, stays as it was)."""
        choice = self.combo.currentData()
        if not isinstance(choice, Choice) or choice.is_group_header:
            return self.original
        picked = (choice.category_id, choice.transfer_account_id)
        if picked == SplitEditor._shown(*self.original):
            return self.original
        return picked

    def cents(self) -> int:
        return round(self.amount.value() * 100)


class SplitEditor(QWidget):
    #: unsaved changes appeared (True) or were saved / reverted (False)
    dirty_changed = Signal(bool)
    #: Save: the parts to store for the shown booking
    save_requested = Signal(object)
    #: Revert: show the booking as stored
    revert_requested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.row: TransactionRow | None = None
        self.choices: list[Choice] = []
        self.parts: list[_Part] = []
        self._original: list[PartKey] = []
        self._dirty = False

        self.list = QWidget()
        self.rows = QVBoxLayout(self.list)
        self.rows.setContentsMargins(0, 0, 0, 0)
        self.rows.setSpacing(8)
        self.rows.addStretch(1)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setWidget(self.list)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setMinimumHeight(96)  # one part; more scroll (laptop at 150 %)

        self.add_button = QPushButton(self.tr("Add part"))
        self.add_button.setToolTip(shortcuts.tip(self.tr("Add part"), "item_new"))
        self.add_button.clicked.connect(lambda: self.add_part())
        self.remaining = QLabel()
        bar = QHBoxLayout()
        bar.addWidget(self.add_button)
        bar.addStretch(1)
        bar.addWidget(self.remaining)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.scroll, 1)
        layout.addLayout(bar)

        # placed by the card at its bottom right, like Save/Revert of the other editors
        self.revert_button = QPushButton(self.tr("Revert"))
        self.revert_button.clicked.connect(self.revert_requested)
        self.save_button = primary(QPushButton(self.tr("Save split")))
        self.save_button.clicked.connect(self.save)
        here = Qt.ShortcutContext.WidgetWithChildrenShortcut
        shortcuts.bind("item_new", self, self._add_part_key, here)
        shortcuts.bind("revert", self, lambda: revert_by_key(self, self.revert_button), here)

    # -- loading -----------------------------------------------------------------------

    def set_choices(self, choices: list[Choice]) -> None:
        self.choices = choices
        for p in self.parts:
            target = p.target()
            fill_choice_combo(p.combo, choices)
            select_choice(p.combo, *self._shown(*target))

    def load(self, row: TransactionRow) -> None:
        """The booking's parts; one that is not split yet starts with its full amount in
        the first part and an empty second one."""
        self.row = row
        for p in self.parts:
            self.rows.removeWidget(p.frame)
            p.frame.hide()  # gives up the focus now, not when deleted later
            p.frame.deleteLater()
        self.parts = []
        if row.splits:
            for s in row.splits:
                self.add_part(s.category_id, s.transfer_account_id, s.amount_cents, s.memo or "")
        else:
            self.add_part(None, None, row.amount_cents)
        self._original = self._keys()
        if len(row.splits) <= 1:
            self.add_part(amount_cents=0)
        self._set_dirty(False)
        self._update()

    @staticmethod
    def _shown(category_id: int | None, transfer_id: int | None) -> tuple[int | None, int | None]:
        """The picker entry of a part: its category, else its transfer account."""
        return (category_id, None) if category_id is not None else (None, transfer_id)

    def _add_part_key(self) -> None:
        """Ctrl+Shift+N: a new part, its category picker focused."""
        if self.add_button.isEnabled():
            self.add_part().combo.setFocus()

    def add_part(
        self,
        category_id: int | None = None,
        transfer_id: int | None = None,
        amount_cents: int | None = None,
        memo: str = "",
    ) -> _Part:
        if amount_cents is None:  # what is left to assign
            amount_cents = self.remaining_cents()
        frame = QFrame()
        frame.setObjectName("splitPart")
        grid = QGridLayout(frame)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(6)
        grid.setVerticalSpacing(4)
        combo = QComboBox()
        combo.setPlaceholderText(self.tr("Category or transfer…"))
        # as narrow as the card: long names are cut, the open list shows them in full
        combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        combo.setMinimumContentsLength(8)
        combo.setMinimumWidth(100)  # its size hint (placeholder, style) is ~230 px
        fill_choice_combo(combo, self.choices)
        select_choice(combo, *self._shown(category_id, transfer_id))
        amount = QDoubleSpinBox()
        amount.setRange(-10_000_000, 10_000_000)
        amount.setDecimals(2)
        amount.setSingleStep(1.0)
        amount.setValue(amount_cents / 100)
        amount.setAlignment(Qt.AlignmentFlag.AlignRight)
        amount.setMinimumWidth(90)
        memo_edit = QLineEdit(memo, placeholderText=self.tr("Memo"))
        memo_edit.setMinimumWidth(40)
        part = _Part(frame, combo, amount, memo_edit, QPushButton(), (category_id, transfer_id))
        part.remove = icon_button(
            ICON_DELETE, "✕", self.tr("Remove part"), lambda: self.remove_part(part)
        )
        part.remove.setAutoDefault(False)
        grid.addWidget(combo, 0, 0, 1, 2)
        grid.addWidget(part.remove, 0, 2)
        grid.addWidget(amount, 1, 0)
        grid.addWidget(memo_edit, 1, 1, 1, 2)
        grid.setColumnStretch(1, 1)
        combo.currentIndexChanged.connect(self._update)
        amount.valueChanged.connect(self._update)
        memo_edit.textEdited.connect(self._update)
        self.rows.insertWidget(len(self.parts), frame)
        self.parts.append(part)
        self._update()
        return part

    def remove_part(self, part: _Part) -> None:
        if len(self.parts) <= 1:
            return
        self.parts.remove(part)
        self.rows.removeWidget(part.frame)
        part.frame.hide()
        part.frame.deleteLater()
        self._update()

    # -- state ---------------------------------------------------------------------------

    def _keys(self) -> list[PartKey]:
        """The parts that count (an empty one with 0.00 is left out)."""
        keys = []
        for p in self.parts:
            cents = p.cents()
            if cents:
                keys.append((*p.target(), cents, p.memo.text().strip()))
        return keys

    def split_parts(self) -> list[SplitInput]:
        return [
            SplitInput(cents, category_id=cat, transfer_account_id=transfer, memo=memo or None)
            for cat, transfer, cents, memo in self._keys()
        ]

    def remaining_cents(self) -> int:
        total = self.row.amount_cents if self.row else 0
        return total - sum(p.cents() for p in self.parts)

    def can_save(self) -> bool:
        return self._dirty and self.remaining_cents() == 0 and bool(self._keys())

    def has_unsaved_changes(self) -> bool:
        return self._dirty

    def _set_dirty(self, dirty: bool) -> None:
        if dirty != self._dirty:
            self._dirty = dirty
            self.dirty_changed.emit(dirty)

    def _update(self, *_args) -> None:
        if self.row is None:
            return
        rest = self.remaining_cents()
        self.remaining.setText(self.tr("Remaining: %1").replace("%1", money(rest)))
        self.remaining.setStyleSheet(f"color: {warning_color().name()};" if rest else "")
        for p in self.parts:
            p.remove.setEnabled(len(self.parts) > 1)
        self._set_dirty(self._keys() != self._original)
        self.save_button.setEnabled(self.can_save())
        # also leaves a split just started (nothing changed yet) for the category again
        self.revert_button.setEnabled(self._dirty or not self.row.is_split)
        self._tab_order()

    def _tab_order(self) -> None:
        chain: list[QWidget] = []
        for p in self.parts:
            chain += [p.combo, p.amount, p.memo, p.remove]
        chain.append(self.add_button)
        for a, b in zip(chain, chain[1:], strict=False):
            QWidget.setTabOrder(a, b)

    def save(self) -> None:
        if self.can_save():
            self.save_requested.emit(self.split_parts())

    def mark_saved(self) -> None:
        self._set_dirty(False)

    def focus_first(self) -> None:
        if self.parts:
            self.parts[0].combo.setFocus()
