"""How a report number adds up: categories with their bookings (see services.drilldown)."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFontMetrics, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QHeaderView,
    QLabel,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

from hiro_budget.gui.charts import month_label, one_off_name
from hiro_budget.gui.common import fit_to_screen, fmt_date, money
from hiro_budget.services import drilldown
from hiro_budget.services.reports import UNCATEGORIZED_KEY

RIGHT = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
TX_ROLE = Qt.ItemDataRole.UserRole + 1
AMOUNT = 4  # column of the amounts


class BreakdownDialog(QDialog):
    """Sections (income / expenses) -> categories -> bookings, with the total at the
    bottom; a booking can be opened on the bookings page."""

    show_booking = Signal(int)

    def __init__(
        self,
        title: str,
        context: str,
        breakdown: drilldown.Breakdown,
        notes: list[str],
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.breakdown = breakdown
        self.setWindowTitle(title)
        fit_to_screen(self, 980, 620)
        heading = QLabel(f"<b>{title}</b> · {money(breakdown.total)}")
        heading.setTextFormat(Qt.TextFormat.RichText)
        sub = QLabel(context)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(5)
        self.tree.setHeaderLabels(
            [
                self.tr("Category / date"),
                self.tr("Payee"),
                self.tr("Purpose"),
                self.tr("Account"),
                self.tr("Amount"),
            ]
        )
        self.tree.headerItem().setTextAlignment(AMOUNT, RIGHT)
        header = self.tree.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.tree.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tree.setUniformRowHeights(True)
        self.tree.itemDoubleClicked.connect(self._double_clicked)
        self.tree.currentItemChanged.connect(self._selection_changed)
        # Return opens a booking or folds a category; without this the tree passes the
        # key on and the dialog's Close button closes the dialog (#64)
        for key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            QShortcut(QKeySequence(key), self.tree, self._return_pressed).setContext(
                Qt.ShortcutContext.WidgetShortcut
            )
        self._fill()

        self.total = QLabel(self._total_text())
        shifted = any(
            b.shifted for sec in breakdown.sections for p in sec.parts for b in p.bookings
        )
        shift_text = (
            self.tr(
                "“→ month”: the booking counts for that month (bookings early in a month count "
                "for the previous one in this category)."
            )
            if shifted
            else ""
        )
        self.notes = QLabel(" ".join(n for n in [*notes, shift_text, self._left_out_text()] if n))
        self.notes.setWordWrap(True)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        self.show_button = QPushButton(self.tr("Show booking"))
        self.show_button.setEnabled(False)
        self.show_button.clicked.connect(lambda: self._open(self.tree.currentItem()))
        buttons.addButton(self.show_button, QDialogButtonBox.ButtonRole.ActionRole)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(heading)
        layout.addWidget(sub)
        layout.addWidget(self.tree, 1)
        layout.addWidget(self.total)
        layout.addWidget(self.notes)
        layout.addWidget(buttons)

    # -- content -----------------------------------------------------------------

    def _section_name(self, kind: str) -> str:
        return self.tr("Income") if kind == "income" else self.tr("Expenses")

    def _category_name(self, part: drilldown.CategoryPart) -> str:
        if part.key == UNCATEGORIZED_KEY:
            return self.tr("Uncategorized")
        return one_off_name(f"{part.group_name}: {part.name}", part.one_off)

    def _row(self, parent, texts: list[str], cents: int, bold: bool = False) -> QTreeWidgetItem:
        item = QTreeWidgetItem([*texts, money(cents)])
        item.setTextAlignment(AMOUNT, RIGHT)
        if bold:
            for col in range(5):
                font = item.font(col)
                font.setBold(True)
                item.setFont(col, font)
        if parent is None:
            self.tree.addTopLevelItem(item)
        else:
            parent.addChild(item)
        return item

    def _count(self, n: int) -> str:
        return self.tr("1 booking") if n == 1 else self.tr("%1 bookings").replace("%1", str(n))

    def _fill(self) -> None:
        sections = self.breakdown.sections
        for section in sections:
            parent = None
            if len(sections) > 1:
                parent = self._row(
                    None, [self._section_name(section.kind), "", "", ""], section.cents, True
                )
            for part in section.parts:
                cat = self._row(
                    parent,
                    [self._category_name(part), self._count(len(part.bookings)), "", ""],
                    part.cents,
                    bold=parent is None,
                )
                for b in part.bookings:
                    date_text = fmt_date(b.booking_date)
                    if b.shifted:
                        date_text += f"  → {month_label(b.report_month)}"
                    row = self._row(cat, [date_text, b.payee, b.purpose, b.account], b.cents)
                    row.setData(0, TX_ROLE, b.tx_id)
                    row.setToolTip(2, b.purpose)
                    if b.shifted:
                        row.setToolTip(
                            0,
                            self.tr("Counts for %1 (reporting month of its category)").replace(
                                "%1", month_label(b.report_month)
                            ),
                        )
            if parent is not None:
                parent.setExpanded(True)
        self._fit_columns()

    def _fit_columns(self) -> None:
        """Widths from all rows, also collapsed ones (resizeColumnToContents only measures
        the visible rows)."""
        widths = {
            col: QFontMetrics(self.tree.headerItem().font(col)).horizontalAdvance(
                self.tree.headerItem().text(col)
            )
            for col in (0, 1, 3, 4)
        }
        indent = self.tree.indentation()
        stack = [(self.tree.topLevelItem(i), 1) for i in range(self.tree.topLevelItemCount())]
        while stack:
            item, depth = stack.pop()
            for col in widths:
                text = item.text(col)
                if text:
                    width = QFontMetrics(item.font(col)).horizontalAdvance(text)
                    widths[col] = max(widths[col], width + (depth * indent if col == 0 else 0))
            stack.extend((item.child(i), depth + 1) for i in range(item.childCount()))
        limits = {0: 380, 1: 240, 3: 200, 4: 140}
        for col, width in widths.items():
            self.tree.setColumnWidth(col, min(width + 24, limits[col]))

    def _total_text(self) -> str:
        sections = {s.kind: s.cents for s in self.breakdown.sections}
        if len(sections) == 2:
            return (
                self.tr("Income %1 − expenses %2 = %3")
                .replace("%1", money(sections["income"]))
                .replace("%2", money(sections["expenses"]))
                .replace("%3", money(self.breakdown.total))
            )
        return self.tr("Total: %1").replace("%1", money(self.breakdown.total))

    def _left_out_text(self) -> str:
        parts = self.breakdown.left_out
        if not parts:
            return ""
        names = ", ".join(f"{self._category_name(p)} {money(p.cents)}" for p in parts)
        return self.tr("Not included (one-offs are left out): %1.").replace("%1", names)

    # -- actions -----------------------------------------------------------------

    def _selection_changed(self, item: QTreeWidgetItem | None, _previous) -> None:
        self.show_button.setEnabled(item is not None and item.data(0, TX_ROLE) is not None)

    def _double_clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        if item.data(0, TX_ROLE) is not None:
            self._open(item)

    def _return_pressed(self) -> None:
        item = self.tree.currentItem()
        if item is None:
            return
        if item.data(0, TX_ROLE) is not None:
            self._open(item)
        elif item.childCount():
            item.setExpanded(not item.isExpanded())

    def _open(self, item: QTreeWidgetItem | None) -> None:
        tx_id = item.data(0, TX_ROLE) if item is not None else None
        if tx_id is not None:
            self.accept()
            self.show_booking.emit(int(tx_id))
