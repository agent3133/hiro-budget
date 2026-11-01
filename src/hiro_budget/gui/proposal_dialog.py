"""Dialog: propose a month's budget from past spending and apply selected amounts."""

from __future__ import annotations

from PySide6.QtCore import QLocale, Qt
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from hiro_budget.app import App
from hiro_budget.gui.common import fit_to_screen, money, show_error
from hiro_budget.services import proposals
from hiro_budget.services.budget import Envelope
from hiro_budget.services.proposals import ProposalResult

(
    COL_USE,
    COL_CATEGORY,
    COL_KIND,
    COL_MONTHS,
    COL_MIN,
    COL_MEDIAN,
    COL_MAX,
    COL_ASSIGNED,
    COL_PROPOSAL,
) = range(9)
TAKE_OVER = (COL_MIN, COL_MEDIAN, COL_ASSIGNED, COL_MAX)  # click: becomes the proposal


class ProposalDialog(QDialog):
    def __init__(self, app: App, month: str, ready_to_assign: int, parent=None) -> None:
        super().__init__(parent)
        self.app = app
        self.month = month
        self.ready_to_assign = ready_to_assign
        self.result_data: ProposalResult | None = None
        self.applied = 0
        self.setWindowTitle(self.tr("Propose budget for %1").replace("%1", self._title(month)))
        fit_to_screen(self, 1050, 640)

        self.months = QSpinBox()
        self.months.setRange(1, 12)
        self.months.setValue(app.settings.app.budget.proposal_months)
        self.months.setSuffix(self.tr(" months"))
        self.months.valueChanged.connect(self.load)
        self.history = QLabel()
        self.history.setWordWrap(True)
        top = QHBoxLayout()
        top.addWidget(QLabel(self.tr("Regular categories: median of the last")))
        top.addWidget(self.months)
        top.addStretch(1)

        self.table = QTableWidget(0, 9)
        self.table.setHorizontalHeaderLabels(
            [
                "",
                self.tr("Category"),
                self.tr("Kind"),
                self.tr("Months with spending"),
                self.tr("Min"),
                self.tr("Median"),
                self.tr("Max"),
                self.tr("Assigned"),
                self.tr("Proposal"),
            ]
        )
        h = self.table.horizontalHeader()
        h.setSectionResizeMode(COL_CATEGORY, QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.cellClicked.connect(self.take_over)
        for col in TAKE_OVER:
            self.table.horizontalHeaderItem(col).setToolTip(
                self.tr("Click an amount to take it over as the proposal.")
            )

        select_all = QPushButton(self.tr("Select all"))
        select_none = QPushButton(self.tr("Select none"))
        select_empty = QPushButton(self.tr("Only empty categories"))
        select_all.clicked.connect(lambda: self._select(lambda r: True))
        select_none.clicked.connect(lambda: self._select(lambda r: False))
        select_empty.clicked.connect(lambda: self._select(lambda r: r.assigned == 0))
        self.summary = QLabel()
        sel_row = QHBoxLayout()
        for b in (select_all, select_none, select_empty):
            sel_row.addWidget(b)
        sel_row.addStretch(1)
        sel_row.addWidget(self.summary)

        explanation = QLabel(
            self.tr(
                "Regular categories (spending in at least two thirds of the months) get the "
                "median, so one-off purchases do not inflate them. Irregular categories get "
                "the total of the last 12 months spread over the months as monthly "
                "provision. Click Min, Median, Max or Assigned to take that amount over "
                "as the proposal. Applying replaces Assigned of the selected categories."
            )
        )
        explanation.setWordWrap(True)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.apply_button = buttons.addButton(
            self.tr("Apply selected"), QDialogButtonBox.ButtonRole.AcceptRole
        )
        self.apply_button.clicked.connect(self._apply)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(self.history)
        layout.addWidget(self.table, 1)
        layout.addLayout(sel_row)
        layout.addWidget(explanation)
        layout.addWidget(buttons)
        self.load()

    def _title(self, month: str) -> str:
        y, m = map(int, month.split("-"))
        return f"{QLocale().standaloneMonthName(m)} {y}"

    # -- data ------------------------------------------------------------------

    def load(self) -> None:
        with self.app.session() as s:
            self.result_data = proposals.propose(
                s, self.app.settings, self.month, months=self.months.value()
            )
        r = self.result_data
        if r.history_months == 0:
            self.history.setText(
                self.tr("No bookings before this month - import older statements first.")
            )
        else:
            text = self.tr("History: %1 – %2 (%3 months)")
            text = (
                text.replace("%1", self._title(r.history_from))
                .replace("%2", self._title(r.history_to))
                .replace("%3", str(r.history_months))
            )
            if r.history_months < 3:
                text += " – " + self.tr("very little history, the proposals are rough estimates.")
            self.history.setText(text)

        self.table.setRowCount(len(r.proposals))
        for i, p in enumerate(r.proposals):
            check = QCheckBox()
            check.setChecked(p.proposal > 0 and p.assigned == 0)
            check.toggled.connect(self._update_summary)
            holder = QWidget()
            hl = QHBoxLayout(holder)
            hl.setContentsMargins(6, 0, 0, 0)
            hl.addWidget(check)
            self.table.setCellWidget(i, COL_USE, holder)

            if p.months_with_spending == 0:
                kind = self.tr("no spending")
            elif p.regular:
                kind = self.tr("regular")
            else:
                kind = self.tr("irregular (provision)")
            months_text = f"{p.months_with_spending} / {len(p.spending)}"
            if p.is_group:
                label = p.name + " " + self.tr("(whole group)")
            else:
                label = f"{p.group_name}: {p.name}"
            cells = {
                COL_CATEGORY: label,
                COL_KIND: kind,
                COL_MONTHS: months_text,
                COL_MIN: money(p.minimum),
                COL_MEDIAN: money(p.median),
                COL_MAX: money(p.maximum),
                COL_ASSIGNED: money(p.assigned),
            }
            link = self.palette().color(QPalette.ColorRole.Link)
            for col, text in cells.items():
                item = QTableWidgetItem(text)
                if col >= COL_MONTHS:
                    item.setTextAlignment(
                        Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
                    )
                if col in TAKE_OVER:
                    item.setForeground(link)  # looks clickable
                    item.setToolTip(self.tr("Click to take over as the proposal"))
                if col == COL_CATEGORY:
                    history = ", ".join(money(v) for v in p.spending)
                    item.setToolTip(self.tr("Spending per month: %1").replace("%1", history))
                if col == COL_CATEGORY and p.is_group:
                    font = item.font()
                    font.setBold(True)
                    item.setFont(font)
                self.table.setItem(i, col, item)

            spin = QDoubleSpinBox()
            spin.setRange(0, 10_000_000)
            spin.setDecimals(2)
            spin.setSuffix(" €")
            spin.setGroupSeparatorShown(True)
            spin.setValue(p.proposal / 100)
            spin.valueChanged.connect(self._update_summary)
            self.table.setCellWidget(i, COL_PROPOSAL, spin)
        self.table.resizeColumnsToContents()
        self.table.horizontalHeader().setSectionResizeMode(
            COL_CATEGORY, QHeaderView.ResizeMode.Stretch
        )
        self.table.setColumnWidth(COL_PROPOSAL, 130)
        self._update_summary()

    def take_over(self, row: int, column: int) -> None:
        """Clicked amount (min, median, max, assigned) becomes the row's proposal."""
        if self.result_data is None or column not in TAKE_OVER:
            return
        p = self.result_data.proposals[row]
        cents = {
            COL_MIN: p.minimum,
            COL_MEDIAN: p.median,
            COL_MAX: p.maximum,
            COL_ASSIGNED: p.assigned,
        }[column]
        self.table.cellWidget(row, COL_PROPOSAL).setValue(max(cents, 0) / 100)
        self._check(row).setChecked(True)

    def _check(self, row: int) -> QCheckBox:
        return self.table.cellWidget(row, COL_USE).findChild(QCheckBox)

    def _amount(self, row: int) -> int:
        return round(self.table.cellWidget(row, COL_PROPOSAL).value() * 100)

    def selected(self) -> dict[Envelope, int]:
        """Envelope (category or group) -> cents for all checked rows."""
        r = self.result_data
        if r is None:
            return {}
        return {
            p.envelope: self._amount(i)
            for i, p in enumerate(r.proposals)
            if self._check(i).isChecked()
        }

    def _select(self, predicate) -> None:
        if self.result_data is None:
            return
        for i, p in enumerate(self.result_data.proposals):
            self._check(i).setChecked(predicate(p) and self._amount(i) > 0)

    def _update_summary(self) -> None:
        r = self.result_data
        if r is None:
            return
        chosen = self.selected()
        # Applying replaces the current amounts of the selected categories.
        current = sum(p.assigned for p in r.proposals if p.envelope in chosen)
        change = sum(chosen.values()) - current
        left = self.ready_to_assign - change
        text = self.tr("%1 categories, %2 – Ready to Assign afterwards: %3")
        self.summary.setText(
            text.replace("%1", str(len(chosen)))
            .replace("%2", money(sum(chosen.values())))
            .replace("%3", money(left))
        )
        self.summary.setStyleSheet("color: #c0392b" if left < 0 else "")
        self.apply_button.setEnabled(bool(chosen))

    def _apply(self) -> None:
        chosen = self.selected()
        try:
            with self.app.session() as s:
                self.applied = proposals.apply_proposals(s, self.month, chosen)
        except Exception as e:
            show_error(self, self.tr("Propose budget"), e)
            return
        self.accept()
