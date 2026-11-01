"""Dialog: assign uncategorized bookings payee by payee, optionally with a rule."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QSplitter,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

from hiro_budget.app import App
from hiro_budget.db.models import Category
from hiro_budget.gui.common import (
    Choice,
    amount_color,
    category_choices,
    fill_choice_combo,
    fit_to_screen,
    fmt_date,
    money,
    select_choice,
    show_error,
)
from hiro_budget.gui.config_view import slugify, unique
from hiro_budget.gui.widgets import DetailCard, banner, primary
from hiro_budget.services import config_edit, payee_review
from hiro_budget.services.payee_review import PayeeGroup
from hiro_budget.services.transactions import TxFilter

GROUP_ROLE = Qt.ItemDataRole.UserRole + 1  # index into self.data (group rows)
TX_ROLE = Qt.ItemDataRole.UserRole + 2  # booking id (booking rows)
RIGHT = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter


class PayeeReviewDialog(QDialog):
    def __init__(self, app: App, tx_filter: TxFilter | None = None, parent=None) -> None:
        super().__init__(parent)
        self.app = app
        self.tx_filter = tx_filter or TxFilter()
        self.data: list[PayeeGroup] = []
        self.choices: list[Choice] = []
        self.assigned = 0  # bookings given a category in this dialog
        self.rules_added = 0
        self.setWindowTitle(self.tr("Assign uncategorized bookings by payee"))
        fit_to_screen(self, 1100, 680)

        hint = banner(
            QLabel(
                self.tr(
                    "Uncategorized bookings, grouped by payee. Choose a category and assign "
                    "the whole group at once; a rule does the same for future imports. "
                    "Untick bookings that belong elsewhere."
                )
            )
        )

        # -- payees, the largest groups first --
        self.groups = QTreeWidget()
        self.groups.setColumnCount(3)
        self.groups.setHeaderLabels([self.tr("Payee"), self.tr("Bookings"), self.tr("Amount")])
        self.groups.setRootIsDecorated(False)
        self.groups.setUniformRowHeights(True)
        self.groups.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        header = self.groups.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for col in (1, 2):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        self.groups.currentItemChanged.connect(lambda *_: self._show_group())

        # -- the selected payee: its bookings, the category, the rule --
        self.card = DetailCard(self.tr("Payee"))
        self.facts = QLabel()
        self.bookings = QTreeWidget()
        self.bookings.setColumnCount(4)
        self.bookings.setHeaderLabels(
            [self.tr("Date"), self.tr("Account"), self.tr("Amount"), self.tr("Purpose")]
        )
        self.bookings.setRootIsDecorated(False)
        self.bookings.setUniformRowHeights(True)
        self.bookings.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.bookings.itemChanged.connect(lambda *_: self._update_buttons())
        self.category = QComboBox()
        self.category.currentIndexChanged.connect(lambda *_: self._update_buttons())
        self.rule = QCheckBox()
        self.rule.setChecked(True)
        category_row = QHBoxLayout()
        category_row.addWidget(QLabel(self.tr("Category")))
        category_row.addWidget(self.category, 1)
        self.skip_button = QPushButton(self.tr("Skip"))
        self.skip_button.clicked.connect(self._next)
        self.skip_button.setAutoDefault(False)  # Enter assigns, never skips
        self.assign_button = primary(QPushButton(self.tr("Assign")))
        self.assign_button.clicked.connect(self.assign)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(self.skip_button)
        buttons.addWidget(self.assign_button)
        body = self.card.body
        body.addWidget(self.facts)
        body.addWidget(self.bookings, 1)
        body.addSpacing(6)
        body.addLayout(category_row)
        body.addWidget(self.rule)
        body.addLayout(buttons)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self.groups)
        splitter.addWidget(self.card)
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 3)

        self.status = QLabel()
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.rejected.connect(self.reject)
        close.button(QDialogButtonBox.StandardButton.Close).setAutoDefault(False)
        bottom = QHBoxLayout()
        bottom.addWidget(self.status, 1)
        bottom.addWidget(close)

        layout = QVBoxLayout(self)
        layout.addWidget(hint)
        layout.addWidget(splitter, 1)
        layout.addLayout(bottom)
        self.load()

    # -- data ----------------------------------------------------------------------

    def load(self) -> None:
        with self.app.session() as s:
            self.data = payee_review.uncategorized_groups(s, self.tx_filter)
            self.choices = category_choices(s)
        fill_choice_combo(self.category, self.choices)
        self.category.insertItem(0, self.tr("Choose a category…"), None)
        self.groups.clear()
        for i, g in enumerate(self.data):
            item = QTreeWidgetItem([g.payee or self.tr("(no payee)"), str(g.count), ""])
            item.setData(0, GROUP_ROLE, i)
            item.setTextAlignment(1, RIGHT)
            item.setTextAlignment(2, RIGHT)
            self.groups.addTopLevelItem(item)
            self._fill_group_item(item, g)
        if self.groups.topLevelItemCount():
            self.groups.setCurrentItem(self.groups.topLevelItem(0))
        self._show_group()
        self._update_status()

    def _fill_group_item(self, item: QTreeWidgetItem, g: PayeeGroup) -> None:
        item.setText(1, str(g.count))
        item.setText(2, money(g.total_cents))
        item.setForeground(2, amount_color(g.total_cents))

    def current_group(self) -> PayeeGroup | None:
        item = self.groups.currentItem()
        return self.data[item.data(0, GROUP_ROLE)] if item else None

    def _checked_ids(self) -> list[int]:
        return [
            self.bookings.topLevelItem(i).data(0, TX_ROLE)
            for i in range(self.bookings.topLevelItemCount())
            if self.bookings.topLevelItem(i).checkState(0) == Qt.CheckState.Checked
        ]

    def _choice(self) -> Choice | None:
        choice = self.category.currentData()
        return choice if isinstance(choice, Choice) and not choice.is_group_header else None

    # -- display -------------------------------------------------------------------

    def _show_group(self) -> None:
        g = self.current_group()
        self.card.setEnabled(g is not None)
        self.bookings.blockSignals(True)
        self.bookings.clear()
        if g is None:
            self.card.set_heading(self.tr("Everything is categorized"))
            self.facts.clear()
            self.bookings.blockSignals(False)
            self._update_buttons()
            return
        self.card.set_heading(g.payee or self.tr("(no payee)"))
        period = fmt_date(g.first)
        if g.last != g.first:
            period += f" – {fmt_date(g.last)}"
        self.facts.setText(
            self.tr("%n booking(s)", "", g.count) + f" · {money(g.total_cents)} · {period}"
        )
        for r in g.rows:
            item = QTreeWidgetItem(
                [fmt_date(r.booking_date), r.account_name, money(r.amount_cents), r.purpose]
            )
            item.setData(0, TX_ROLE, r.id)
            item.setCheckState(0, Qt.CheckState.Checked)
            item.setTextAlignment(2, RIGHT)
            item.setForeground(2, amount_color(r.amount_cents))
            item.setToolTip(3, r.purpose)
            self.bookings.addTopLevelItem(item)
        for col in range(3):
            self.bookings.resizeColumnToContents(col)
        self.bookings.blockSignals(False)
        if g.suggestion is not None:  # what this payee was given before
            select_choice(self.category, g.suggestion, None)
        else:
            self.category.setCurrentIndex(0)
        self.rule.setText(
            self.tr('Create a rule: future bookings of "%1" get this category').replace(
                "%1", g.source
            )
        )
        self._update_buttons()

    def _update_buttons(self) -> None:
        g = self.current_group()
        choice = self._choice()
        self.assign_button.setEnabled(bool(g and choice and self._checked_ids()))
        # rules give categories, not transfers; and they need a payee to match
        self.rule.setEnabled(bool(g and g.source and (choice is None or choice.category_id)))

    def _update_status(self) -> None:
        left = sum(g.count for g in self.data)
        text = self.tr("%n payee(s) left", "", len(self.data))
        text += " · " + self.tr("%n booking(s)", "", left)
        if self.assigned:
            text += " · " + self.tr("%n assigned", "", self.assigned)
        if self.rules_added:
            text += " · " + self.tr("%n new rule(s)", "", self.rules_added)
        self.status.setText(text)

    # -- actions -------------------------------------------------------------------

    def assign(self) -> None:
        g, choice, ids = self.current_group(), self._choice(), self._checked_ids()
        if g is None or choice is None or not ids:
            return
        with_rule = self.rule.isEnabled() and self.rule.isChecked() and choice.category_id
        try:
            with self.app.session() as s:
                payee_review.assign(
                    s,
                    ids,
                    category_id=choice.category_id,
                    transfer_account_id=choice.transfer_account_id,
                    days=self.app.settings.app.matching.days,
                )
                key = s.get_one(Category, choice.category_id).key if with_rule else None
            if key:
                rules = self.app.settings.rules.rules
                rule_id = unique(slugify(g.source)[:40], {r.id for r in rules})
                rule = payee_review.payee_rule(g, key, rule_id)
                config_edit.save_rules(self.app.settings, [rule, *rules])  # first match wins
                self.rules_added += 1
        except Exception as e:
            show_error(self, self.tr("Assign"), e)
            return
        self.assigned += len(ids)
        g.rows = [r for r in g.rows if r.id not in ids]
        item = self.groups.currentItem()
        if g.rows:  # unticked bookings stay for another category
            self._fill_group_item(item, g)
            self._show_group()
        else:
            self._remove_current()
        self._update_status()

    def _remove_current(self) -> None:
        item = self.groups.currentItem()
        row = self.groups.indexOfTopLevelItem(item)
        self.data[item.data(0, GROUP_ROLE)].rows = []
        self.groups.takeTopLevelItem(row)
        self.data = [g for g in self.data if g.rows]
        for i in range(self.groups.topLevelItemCount()):  # indexes after the removal
            it = self.groups.topLevelItem(i)
            it.setData(0, GROUP_ROLE, i)
        count = self.groups.topLevelItemCount()
        if count:
            self.groups.setCurrentItem(self.groups.topLevelItem(min(row, count - 1)))
        else:
            self.groups.setCurrentItem(None)
        self._show_group()

    def _next(self) -> None:
        row = self.groups.indexOfTopLevelItem(self.groups.currentItem())
        if row + 1 < self.groups.topLevelItemCount():
            self.groups.setCurrentItem(self.groups.topLevelItem(row + 1))
