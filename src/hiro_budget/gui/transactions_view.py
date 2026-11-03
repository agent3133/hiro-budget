"""Transactions tab: filterable table, inline category editing, context actions."""

from __future__ import annotations

import html
from collections.abc import Callable
from dataclasses import replace
from datetime import date

from PySide6.QtCore import (
    QByteArray,
    QEvent,
    QModelIndex,
    QPersistentModelIndex,
    QRectF,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import QAction, QColor, QFont, QFontMetrics, QPalette
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QDateEdit,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QSplitter,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from hiro_budget.app import App
from hiro_budget.gui import shortcuts, theme
from hiro_budget.gui.booking_card import BookingCard
from hiro_budget.gui.booking_dialog import BookingDialog
from hiro_budget.gui.common import (
    Choice,
    category_choices,
    flat_table,
    from_qdate,
    money,
    show_error,
    to_qdate,
)
from hiro_budget.gui.date_range import DateRangeButton
from hiro_budget.gui.models.transaction_table_model import (
    ROW_ROLE,
    Col,
    ModelSortProxy,
    TransactionTableModel,
)
from hiro_budget.gui.payee_review_dialog import PayeeReviewDialog
from hiro_budget.gui.widgets import (
    QuickFilterButton,
    action_bar,
    banner,
    page_title,
    paint_chip,
    primary,
)
from hiro_budget.services import lookups
from hiro_budget.services import transactions as txs
from hiro_budget.services.networth import running_balances
from hiro_budget.services.reports import booking_date_range
from hiro_budget.services.transactions import TransactionRow, TxFilter


class CategoryDelegate(QStyledItemDelegate):
    """The category column: editing (double-click, Enter) opens the category menu with
    one submenu per group instead of an editor."""

    def __init__(self, view: TransactionsView) -> None:
        super().__init__(view)
        self.view = view

    def paint(self, painter, option, index):
        """Rosé Pine: an uncategorized booking shows a dashed "+ Category" pill."""
        t = theme.tokens()
        r: TransactionRow | None = index.data(ROW_ROLE)
        if t is None or r is None or not r.is_uncategorized:
            super().paint(painter, option, index)
            return
        text = self.tr("+ Category")
        paint_chip(painter, option, text, t["notice"], t["notice_text"], t["warn_text"])

    def createEditor(self, parent, option, index):
        # after the event that started editing: the menu runs its own event loop
        row = QPersistentModelIndex(index)
        QTimer.singleShot(0, lambda: self.view.open_category_menu(QModelIndex(row)))
        return None


SOURCE_WIDTH = 120  # last column, it takes the rest of the width


class TwoLineDelegate(QStyledItemDelegate):
    """Two lines in a cell: the column's text, below it a second text in muted ink
    (payee over purpose, account over booking type)."""

    def __init__(self, second, bold: bool, parent=None) -> None:
        super().__init__(parent)
        self.second = second  # TransactionRow -> text of the second line
        self.bold = bold

    def paint(self, painter, option, index):
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        first = opt.text
        opt.text = ""  # the style draws background and selection, the lines are ours
        style = opt.widget.style() if opt.widget else QApplication.style()
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, opt, painter, opt.widget)
        row = index.data(ROW_ROLE)
        rect = QRectF(opt.rect).adjusted(6, 3, -6, -3)
        half = rect.height() / 2
        painter.save()
        top = QFont(opt.font)
        if self.bold:
            top.setWeight(QFont.Weight.DemiBold)
        selected = opt.state & QStyle.StateFlag.State_Selected
        ink = opt.palette.color(
            QPalette.ColorRole.HighlightedText if selected else QPalette.ColorRole.Text
        )
        painter.setFont(top)
        painter.setPen(ink)
        width = int(rect.width())
        elide = Qt.TextElideMode.ElideRight
        align = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        painter.drawText(
            QRectF(rect.left(), rect.top(), rect.width(), half),
            align,
            QFontMetrics(top).elidedText(first, elide, width),
        )
        below = QFont(opt.font)
        below.setPointSizeF(max(6.0, below.pointSizeF() - 0.5))
        t = theme.tokens()
        muted = QColor(t["muted"]) if t else opt.palette.color(QPalette.ColorRole.PlaceholderText)
        painter.setFont(below)
        painter.setPen(muted)
        text = self.second(row) if row is not None else ""
        painter.drawText(
            QRectF(rect.left(), rect.top() + half, rect.width(), half),
            align,
            QFontMetrics(below).elidedText(" ".join(text.split()), elide, width),
        )
        painter.restore()


CARD_MIN, CARD_MAX = 280, 340  # default width of the booking card
PURPOSE_MIN_WIDTH = 160  # the purpose column gives way to the others down to this


class TransactionsView(QWidget):
    #: emitted after bookings were changed (reports need a refresh)
    data_changed = Signal()
    #: emitted to open the rule editor pre-filled from a booking
    create_rule_requested = Signal(object)
    #: rules were added (assigning by payee)
    rules_changed = Signal()

    def __init__(self, app: App, parent=None) -> None:
        super().__init__(parent)
        self.app = app
        self.choices: list[Choice] = []
        self._dates_initialized = False

        bar = self._build_filter_bar()
        quick_row = self._build_quick_filters()
        self._build_table()
        self._build_bars()
        self._bind_keys()
        self._build_card()

        self.summary = QLabel()

        self.splitter = splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self.table)
        splitter.addWidget(self.card)
        # a wider window widens the list, the card keeps its width (#41)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 0)
        splitter.setCollapsible(0, False)  # the card can be dragged away, the list not
        splitter_state = app.ui_state.get("transactions.splitter", "")
        # the position the user dragged to is kept (save_state); else a default once the
        # splitter has its width (showEvent)
        self._default_split = not splitter_state
        if splitter_state:
            splitter.restoreState(QByteArray.fromBase64(splitter_state.encode()))

        layout = QVBoxLayout(self)
        layout.addWidget(page_title(self.tr("Bookings")))
        layout.addLayout(bar)
        layout.addLayout(quick_row)
        layout.addWidget(self.subset_bar)
        layout.addWidget(self.review_bar)
        layout.addWidget(splitter, 1)
        layout.addLayout(
            action_bar(
                [
                    self.new_button,
                    self.payee_button,
                    self.accept_selected_button,
                    self.accept_all_button,
                ],
                end=self.summary,
            )
        )

        self._connect()
        self.reload_filters()

    def _build_filter_bar(self) -> QHBoxLayout:
        """Search, period, account and category above the list."""
        # -- filter bar --
        self.date_from = QDateEdit(calendarPopup=True)
        self.date_to = QDateEdit(calendarPopup=True)
        for d in (self.date_from, self.date_to):
            d.setDisplayFormat("dd.MM.yyyy")
        self.account = QComboBox()
        self.category = QComboBox()
        self.category.setMinimumWidth(220)
        self.search = QLineEdit(placeholderText=self.tr("Search payee, purpose, notes, amount…"))
        self.search.setClearButtonEnabled(True)

        self.period = DateRangeButton(self.date_from, self.date_to, self._full_range)
        # as in the design: search first, then the drop-downs (they say what they filter)
        self.account.setToolTip(self.tr("Account"))
        self.category.setToolTip(self.tr("Category"))
        bar = QHBoxLayout()
        bar.addWidget(self.search, 1)
        bar.addWidget(self.period)
        bar.addWidget(self.account)
        bar.addWidget(self.category)
        self.new_button = QPushButton(self.tr("New booking…"))
        self.new_button.setToolTip(self.tr("Enter an expense, income or transfer by hand (Ctrl+N)"))
        self.new_button.clicked.connect(lambda: self.new_booking())
        primary(self.new_button)
        return bar

    def _build_quick_filters(self) -> QHBoxLayout:
        """The quick filters with their counts, as tabs under the filter bar."""
        # -- quick filters with their counts, as tabs under the filter bar --
        self.quick = {
            txs.ALL: QuickFilterButton(self.tr("All")),
            txs.UNCATEGORIZED: QuickFilterButton(self.tr("Uncategorized"), warn=True),
            txs.REVIEW: QuickFilterButton(self.tr("To review"), warn=True),
        }
        self.quick[txs.REVIEW].setToolTip(
            self.tr("Bookings the classifier categorized, to check and accept them")
        )
        for key, button in self.quick.items():
            button.clicked.connect(lambda _=False, key=key: self._quick_filter(key))
        self.payee_button = QPushButton(self.tr("Assign by payee…"))
        self.payee_button.setToolTip(
            self.tr(
                "Uncategorized bookings grouped by payee: a category for all of a payee's "
                "bookings at once, and a rule for future ones"
            )
        )
        self.payee_button.clicked.connect(self.assign_by_payee)
        quick_row = QHBoxLayout()
        quick_row.setSpacing(4)
        for button in self.quick.values():
            quick_row.addWidget(button)
        quick_row.addStretch(1)
        return quick_row

    def _build_table(self) -> None:
        """The bookings table: model, columns, saved header, one or two lines."""
        # -- table --
        self.model = TransactionTableModel(self)
        self.proxy = ModelSortProxy(self)
        self.proxy.setSourceModel(self.model)
        self.table = QTableView()
        flat_table(self.table)
        self.table.setModel(self.proxy)
        self.table.setSortingEnabled(True)
        self.table.sortByColumn(Col.DATE, Qt.SortOrder.DescendingOrder)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        # F2 / Enter are shortcuts that always edit the category (see below), whichever
        # column the cursor is in; Qt's own edit key would edit the current column.
        self.table.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.SelectedClicked
        )
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setWordWrap(False)
        self.table.setItemDelegateForColumn(Col.CATEGORY, CategoryDelegate(self))
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._context_menu)
        header = self.table.horizontalHeader()
        for col, width in {
            Col.DATE: 90,
            Col.ACCOUNT: 120,
            Col.TYPE: 110,
            Col.PAYEE: 220,
            Col.AMOUNT: 100,
            Col.CATEGORY: 190,
            Col.SOURCE: SOURCE_WIDTH,
        }.items():
            self.table.setColumnWidth(col, width)
        self.table.setColumnWidth(Col.BALANCE, 110)
        header_state = self.app.ui_state.get("transactions.header", "")
        if header_state:
            header.restoreState(QByteArray.fromBase64(header_state.encode()))
        if header.visualIndex(Col.BALANCE) == header.count() - 1:  # not placed yet
            header.moveSection(header.visualIndex(Col.BALANCE), header.visualIndex(Col.AMOUNT) + 1)
        self.table.setColumnHidden(Col.BALANCE, True)  # while one account is filtered
        # Every border can be dragged (also in widths saved by older versions, where the
        # purpose column had an automatic width); the last column takes the rest.
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(True)
        self.stale = False  # data changed while hidden: reload_filters when shown (#56)
        self._columns_fitted = bool(header_state)  # else: purpose fills the window (showEvent)
        self.table.viewport().installEventFilter(self)  # resized: purpose fits (eventFilter)
        self._one_line_height = self.table.verticalHeader().defaultSectionSize()
        self._two_line_delegates = {
            Col.PAYEE: TwoLineDelegate(lambda r: r.purpose, bold=True, parent=self),
            Col.ACCOUNT: TwoLineDelegate(lambda r: r.booking_type, bold=False, parent=self),
        }
        self.two_lines = False
        self.set_two_lines(bool(self.app.ui_state.get("transactions.two_lines", True)))

    def _build_bars(self) -> None:
        """The subset bar and the review bar above the list (hidden at first)."""
        # -- subset bar (shown while only given bookings are shown, e.g. a rule's hits) --
        self._ids: tuple[int, ...] | None = None
        self._ids_label = ""
        self.subset_bar = banner(QLabel(), "info")
        self.subset_bar.linkActivated.connect(lambda _link: self.show_ids(None))
        self.subset_bar.hide()

        # -- review bar (shown with the "suggestions to review" filter) --
        self.review_bar = QWidget()
        review_layout = QHBoxLayout(self.review_bar)
        review_layout.setContentsMargins(0, 0, 0, 0)
        review_hint = QLabel(
            self.tr(
                "Check the suggested categories. Accept the right ones (Enter); choose "
                "another category by double-clicking it, which confirms it as well."
            )
        )
        review_hint.setWordWrap(True)
        # its buttons are in the bar below the list, shown while reviewing
        self.accept_selected_button = QPushButton(self.tr("Accept selected"))
        self.accept_selected_button.clicked.connect(self.accept_selected)
        self.accept_all_button = QPushButton()
        self.accept_all_button.clicked.connect(self.accept_all_shown)
        review_layout.addWidget(review_hint, 1)
        self.review_bar.hide()
        for b in (self.accept_selected_button, self.accept_all_button):
            b.hide()

    def _bind_keys(self) -> None:
        """Keys on the list and on the page."""
        # keys on the list (not while a cell is being edited)
        on_list = Qt.ShortcutContext.WidgetShortcut
        # Return (and Enter): accept a suggestion while reviewing, else the category; F2
        # always the category
        for index, slot in ((0, self._return_pressed), (1, self.edit_category)):
            shortcuts.bind("booking_category", self.table, slot, on_list, index=index)
        shortcuts.bind("booking_category", self.table, self._return_pressed, on_list, index=2)
        shortcuts.bind("delete_bookings", self.table, self._delete_key, on_list)
        shortcuts.bind(
            "split", self, self._split_selected, Qt.ShortcutContext.WidgetWithChildrenShortcut
        )
        shortcuts.bind("search_to_list", self.search, self.table.setFocus, on_list)
        page = Qt.ShortcutContext.WidgetWithChildrenShortcut
        for index, key in enumerate((txs.ALL, txs.UNCATEGORIZED, txs.REVIEW)):
            shortcuts.bind(
                "quick_filter", self, lambda k=key: self._quick_filter(k), page, index=index
            )
        shortcuts.bind("clear_filters", self, lambda: self.show_selection(txs.ALL), page)
        for index, step in ((0, -1), (1, 1)):
            shortcuts.bind(
                "period_step", self, lambda s=step: self.period.step(s), page, index=index
            )
        self.setFocusProxy(self.table)

    def _build_card(self) -> None:
        """The selected booking as a card on the right, and its keys."""
        # -- the selected booking as a card on the right --
        self.card = BookingCard()
        self.notes = self.card.notes
        self.notes.editingFinished.connect(self._save_note)
        self._notes_tx_id: int | None = None
        self.card.category_chosen.connect(self._card_category)
        self.card.rule_requested.connect(self._card_rule)
        # the card: Esc back to the list, Ctrl+E the note, Ctrl+R a rule (#69)
        here = Qt.ShortcutContext.WidgetWithChildrenShortcut
        shortcuts.bind("search_to_list", self.card, self.table.setFocus, here)
        shortcuts.bind("note", self, self._note_key, here)
        shortcuts.bind("create_rule", self, self._rule_key, here)
        self.card.rule_button.setToolTip(
            shortcuts.tip(self.card.rule_button.toolTip(), "create_rule")
        )
        self.card.payee_requested.connect(self.show_ids)
        self.card.editor.save_requested.connect(self._save_split)
        self.card.editor.revert_requested.connect(self._back_to_list)
        self._refreshing = False  # the list is reloaded: no question about the split
        self._staying = False  # back to the booking whose split is being edited

    def _connect(self) -> None:
        """Filters reload the list; the selection fills the card."""
        # -- signals --
        self._search_timer = QTimer(self, singleShot=True, interval=250)
        self._search_timer.timeout.connect(self.refresh)
        self.search.textChanged.connect(self._search_timer.start)
        for w in (self.date_from, self.date_to):
            w.dateChanged.connect(self.refresh)
        self.account.currentIndexChanged.connect(self.refresh)
        self.category.currentIndexChanged.connect(self.refresh)
        self.table.selectionModel().selectionChanged.connect(self._show_details)

    # -- filters ---------------------------------------------------------------

    def reload_filters(self) -> None:
        """Rebuild filter combos (after import or config changes) and refresh the table."""
        self.stale = False
        widgets = (self.date_from, self.date_to, self.account, self.category)
        for w in widgets:
            w.blockSignals(True)
        try:
            with self.app.session() as s:
                first, last = booking_date_range(s)
                self.choices = category_choices(s)
                self.card.set_choices(self.choices)
                filter_choices = category_choices(s, include_transfers=False)
                accounts = lookups.accounts(s)
                prev_acc = self.account.currentData()
                self.account.clear()
                self.account.addItem(self.tr("All accounts"), None)
                for a in accounts:
                    self.account.addItem(a.name, a.id)
                if prev_acc is not None:
                    self.account.setCurrentIndex(max(0, self.account.findData(prev_acc)))

            prev_cat = self.category.currentData()
            self.category.clear()
            self.category.addItem(self.tr("All categories"), txs.ALL)
            self.category.addItem(self.tr("⚠ Uncategorized"), txs.UNCATEGORIZED)
            self.category.addItem(self.tr("? Suggestions to review"), txs.REVIEW)
            self.category.addItem(self.tr("↔ Transfers"), txs.TRANSFERS)
            for ch in filter_choices:
                if ch.is_group_header:  # a group: all its categories
                    self.category.addItem(ch.label, txs.group_filter(ch.group_id))
                    item = self.category.model().item(self.category.count() - 1)
                    font = item.font()
                    font.setBold(True)
                    item.setFont(font)
                else:
                    self.category.addItem(ch.label, ch.category_id)
            if prev_cat is not None:
                self.category.setCurrentIndex(max(0, self.category.findData(prev_cat)))

            today = date.today()
            if first is None:
                first = last = today
            if not self._dates_initialized:
                self.date_from.setDate(to_qdate(first))
                self.date_to.setDate(to_qdate(max(last, today)))
                self._dates_initialized = True
            else:
                # Extend the range if new data arrived outside of it.
                if to_qdate(first) < self.date_from.date():
                    self.date_from.setDate(to_qdate(first))
                if to_qdate(last) > self.date_to.date():
                    self.date_to.setDate(to_qdate(last))
        finally:
            for w in widgets:
                w.blockSignals(False)
        self.period.refresh()  # the dates were set quietly
        self.refresh()

    def current_filter(self) -> TxFilter:
        return TxFilter(
            date_from=from_qdate(self.date_from.date()),
            date_to=from_qdate(self.date_to.date()),
            account_id=self.account.currentData(),
            category=self.category.currentData() or txs.ALL,
            text=self.search.text(),
            ids=self._ids,
            ids_label=self._ids_label,
        )

    def show_ids(self, ids: list[int] | None, label: str = "") -> None:
        """Only these bookings (over the whole period, other filters reset), or (None)
        all again; ``label`` says above the list what they are."""
        if ids is None:
            self._ids, self._ids_label = None, ""
            self._update_subset_bar()
            self.refresh()
            return
        span = self._full_range()
        start, end = span if span else (None, None)
        self.restore_nav_state((TxFilter(start, end, ids=tuple(ids), ids_label=label), None))

    def _update_subset_bar(self) -> None:
        if self._ids is None:
            self.subset_bar.hide()
            return
        self.subset_bar.setText(
            self.tr("Only %1 (%2 bookings). <a href='#'>Show all bookings</a>")
            .replace("%1", html.escape(self._ids_label))
            .replace("%2", str(len(self._ids)))
        )
        self.subset_bar.show()

    def show_selection(
        self,
        category: str | int = txs.ALL,
        date_from: date | None = None,
        date_to: date | None = None,
    ) -> None:
        """Show bookings of a category (and period), e.g. from the budget view."""
        widgets = (self.date_from, self.date_to, self.account, self.category, self.search)
        for w in widgets:
            w.blockSignals(True)
        try:
            self._ids, self._ids_label = None, ""
            self._update_subset_bar()
            self.account.setCurrentIndex(0)
            self.search.clear()
            if date_from is not None:
                self.date_from.setDate(to_qdate(date_from))
            if date_to is not None:
                self.date_to.setDate(to_qdate(date_to))
            self.category.setCurrentIndex(max(0, self.category.findData(category)))
        finally:
            for w in widgets:
                w.blockSignals(False)
        self.period.refresh()  # the dates were set quietly
        self.refresh()

    def show_account(self, account_id: int) -> None:
        """All bookings of an account, over the whole period."""
        span = self._full_range()
        start, end = span if span else (None, None)
        self.restore_nav_state((TxFilter(start, end, account_id=account_id), None))

    def nav_state(self) -> tuple[TxFilter, int | None]:
        """Filter and selected booking, for going back here (history)."""
        rows = self.selected_rows()
        return self.current_filter(), rows[0].id if rows else None

    def restore_nav_state(self, state: tuple[TxFilter, int | None]) -> None:
        f, booking = state
        widgets = (self.date_from, self.date_to, self.account, self.category, self.search)
        for w in widgets:
            w.blockSignals(True)
        try:
            self._ids, self._ids_label = f.ids, f.ids_label
            self._update_subset_bar()
            if f.date_from and f.date_to:
                self.date_from.setDate(to_qdate(f.date_from))
                self.date_to.setDate(to_qdate(f.date_to))
            self.account.setCurrentIndex(max(0, self.account.findData(f.account_id)))
            self.category.setCurrentIndex(max(0, self.category.findData(f.category)))
            self.search.setText(f.text)
        finally:
            for w in widgets:
                w.blockSignals(False)
        self.period.refresh()
        self.refresh()
        self.table.clearSelection()
        if booking is not None:
            self._select_id(booking)

    def _update_quick(self) -> None:
        """Counts of the quick filters (with the other filters as set) and the active one."""
        f = self.current_filter()
        with self.app.session() as s:
            for key, button in self.quick.items():
                button.set_count(txs.count_transactions(s, replace(f, category=key)))
        for key, button in self.quick.items():
            button.setChecked(f.category == key)
        self.payee_button.setVisible(self.quick[txs.UNCATEGORIZED].count > 0)

    def focus_regions(self) -> list[QWidget]:
        """F6 goes round these: filters, quick filters, list, the card (#69)."""
        return [self.search, self.quick[txs.ALL], self.table, self.card.category]

    def _note_key(self) -> None:
        if self.notes.isVisible() and self.notes.isEnabled():
            self.notes.setFocus()
            self.notes.selectAll()

    def _rule_key(self) -> None:
        if self.card.rule_button.isVisible() and self.card.rule_button.isEnabled():
            self.card.rule_button.click()

    def _quick_filter(self, key: str) -> None:
        if key == txs.REVIEW:
            self.start_review()  # over the whole period, the first suggestion selected
        else:
            self.show_filter(key)
        self._update_quick()  # also when the filter was active already (click unchecks)

    def assign_by_payee(self) -> None:
        """The uncategorized bookings of the shown period and account, payee by payee."""
        f = replace(self.current_filter(), category=txs.UNCATEGORIZED)
        dialog = PayeeReviewDialog(self.app, f, self)
        dialog.exec()
        if dialog.rules_added:
            self.rules_changed.emit()
        if dialog.assigned:
            self.refresh()
            self._show_details()
            self.data_changed.emit()

    def show_filter(self, category: str | int) -> None:
        idx = self.category.findData(category)
        if idx >= 0:
            self.category.setCurrentIndex(idx)

    # -- table -----------------------------------------------------------------

    def refresh(self) -> None:
        selected = {r.id for r in self.selected_rows()}
        f = self.current_filter()
        with self.app.session() as s:
            rows = txs.list_transactions(s, f)
            # one account: its balance after each booking, as on its statements (#45)
            balances = running_balances(s, f.account_id) if f.account_id else None
        self.table.setColumnHidden(Col.BALANCE, balances is None)
        self._refreshing = True  # the selection is cleared and restored on the way
        try:
            self.model.set_rows(rows, balances)
            self._update_review()
            if selected:
                sel = self.table.selectionModel()
                for i in range(self.proxy.rowCount()):
                    idx = self.proxy.index(i, 0)
                    if idx.data(ROW_ROLE).id in selected:
                        sel.select(
                            idx,
                            sel.SelectionFlag.Select | sel.SelectionFlag.Rows,
                        )
        finally:
            self._refreshing = False
        self._show_details()
        self._update_summary()

    def _update_review(self) -> None:
        self._update_quick()
        reviewing = self.current_filter().category == txs.REVIEW
        self.review_bar.setVisible(reviewing)
        self.accept_selected_button.setVisible(reviewing)
        self.accept_all_button.setVisible(reviewing)
        shown = sum(1 for r in self.model.rows() if r.needs_review)
        self.accept_all_button.setText(self.tr("Accept all shown (%1)").replace("%1", str(shown)))
        self.accept_all_button.setEnabled(shown > 0)

    def showEvent(self, event) -> None:
        if self.stale:
            self.reload_filters()
        super().showEvent(event)
        if self._default_split:
            self._default_split = False
            self._set_default_split()
        if not self._columns_fitted:
            self._columns_fitted = True
            QTimer.singleShot(0, self._fit_purpose)  # once the table has its final width

    def _set_default_split(self) -> None:
        """The card at 22 % of the width, between CARD_MIN and CARD_MAX: about a fifth
        on a laptop, no wider than its content needs on a large screen."""
        total = sum(self.splitter.sizes()) or self.splitter.width()
        card = max(CARD_MIN, min(CARD_MAX, round(total * 0.22)))
        self.splitter.setSizes([max(total - card, 1), card])

    def set_two_lines(self, on: bool) -> None:
        """Two lines per booking (payee over purpose, account over type) or one line
        with separate type and purpose columns; remembered."""
        self.two_lines = on
        self.model.set_two_lines(on)
        for col, delegate in self._two_line_delegates.items():
            self.table.setItemDelegateForColumn(col, delegate if on else None)
        for col in (Col.TYPE, Col.PURPOSE):
            self.table.setColumnHidden(col, on)
        rows = self.table.verticalHeader()
        if on:
            rows.setDefaultSectionSize(2 * self.table.fontMetrics().height() + 14)
        else:
            rows.setDefaultSectionSize(self._one_line_height)
        self.app.ui_state.set("transactions.two_lines", on)
        self._fit_purpose()

    def _flexible_column(self) -> Col:
        """The column that takes the free width: purpose, or payee with two lines."""
        return Col.PAYEE if self.two_lines else Col.PURPOSE

    def _fit_purpose(self) -> None:
        """The flexible column (purpose, or payee with two lines) takes the free width,
        so all columns fit (first start, and whenever the list gets wider or narrower:
        window, booking card); the other columns keep the widths they were dragged to."""
        if not self.table.isVisible():
            return  # not laid out yet: fitted when shown (resize)
        h = self.table.horizontalHeader()
        flexible = self._flexible_column()
        last = h.logicalIndex(h.count() - 1)  # it stretches
        others = sum(
            h.sectionSize(c)
            for c in range(h.count())
            if c not in (flexible, last) and not h.isSectionHidden(c)
        )
        # the last column stretches over the rest: count it with its normal width
        free = self.table.viewport().width() - others - SOURCE_WIDTH
        self.table.setColumnWidth(flexible, max(PURPOSE_MIN_WIDTH, free))

    def eventFilter(self, obj, event) -> bool:
        if obj is self.table.viewport() and event.type() == QEvent.Type.Resize:
            self._fit_purpose()
        return super().eventFilter(obj, event)

    def save_state(self) -> None:
        header = self.table.horizontalHeader().saveState()
        self.app.ui_state.set("transactions.header", header.toBase64().data().decode())
        splitter = self.splitter.saveState().toBase64().data().decode()
        self.app.ui_state.set("transactions.splitter", splitter)

    def _update_summary(self) -> None:
        rows = self.model.rows()
        income = sum(r.amount_cents for r in rows if r.amount_cents > 0)
        expenses = sum(r.amount_cents for r in rows if r.amount_cents < 0)
        uncategorized = sum(1 for r in rows if r.is_uncategorized)
        review = sum(1 for r in rows if r.needs_review)
        self.summary.setText(
            self.tr("%1 bookings · inflow %2 · outflow %3 · %4 uncategorized · %5 to review")
            .replace("%1", str(len(rows)))
            .replace("%2", money(income))
            .replace("%3", money(expenses))
            .replace("%4", str(uncategorized))
            .replace("%5", str(review))
        )

    def selected_rows(self) -> list[TransactionRow]:
        sel = self.table.selectionModel()
        if sel is None:
            return []
        return [idx.data(ROW_ROLE) for idx in sel.selectedRows()]

    def _save_note(self) -> None:
        tx_id = self._notes_tx_id
        if tx_id is None:
            return
        row = next((r for r in self.model.rows() if r.id == tx_id), None)
        text = self.notes.text().strip()
        if row is None or text == (row.notes or ""):
            return
        if self._run(self.tr("Note"), lambda s: txs.set_notes(s, tx_id, text)):
            with self.app.session() as s:
                self.model.replace_row(txs.get_row(s, tx_id))

    def _show_details(self) -> None:
        if self._refreshing or self._staying:
            return
        self._save_note()  # keep a note typed before the selection changed
        rows = self.selected_rows()
        shown = self.card.row
        if (
            shown is not None
            and self.card.editor.has_unsaved_changes()
            and not (len(rows) == 1 and rows[0].id == shown.id)
            and not self._leave_split(shown)
        ):
            return
        if len(rows) != 1:
            self._notes_tx_id = None
            self.card.show_rows(rows)
            return
        r = rows[0]
        self._notes_tx_id = r.id
        parts = self.model.split_tooltip(r) if r.is_split else ""
        self.card.show_rows([r], parts, self.model.source_label(r))
        with self.app.session() as s:
            self.card.show_history(txs.payee_history(s, r.id))

    def _leave_split(self, shown: TransactionRow) -> bool:
        """The selection moves away from a booking with unsaved parts: save them, discard
        them, or stay (False)."""
        editor = self.card.editor
        buttons = QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel
        if editor.can_save():
            buttons |= QMessageBox.StandardButton.Save
        answer = QMessageBox.question(
            self,
            self.tr("Unsaved split"),
            self.tr("The split of “%1” has unsaved changes.").replace("%1", shown.payee),
            buttons,
            QMessageBox.StandardButton.Cancel,
        )
        if answer == QMessageBox.StandardButton.Save:
            return self._save_split(editor.split_parts(), shown.id)
        if answer == QMessageBox.StandardButton.Discard:
            editor.mark_saved()
            return True
        self._staying = True  # back to it; the card still shows it if the list does not
        try:
            self.table.clearSelection()
            self._select_id(shown.id)
        finally:
            self._staying = False
        return False

    def _back_to_list(self) -> None:
        """After Revert (or saving a booking unsplit) the editor may be gone: focus the
        list instead of a hidden field."""
        if not self.card.editing:
            self.table.setFocus()

    def _split_selected(self) -> None:
        rows = self.selected_rows()
        if len(rows) == 1:
            self.split(rows[0])

    def _save_split(self, parts: list, tx_id: int | None = None) -> bool:
        if tx_id is None:
            if self.card.row is None:
                return False
            tx_id = self.card.row.id
        transfer = any(p.transfer_account_id for p in parts)
        if not self._run(
            self.tr("Split booking"), lambda s: txs.set_splits(s, tx_id, parts, self._days)
        ):
            return False
        self.card.editor.mark_saved()
        self._after_change([tx_id], transfer)
        self._back_to_list()
        return True

    def has_unsaved_changes(self) -> bool:
        return self.card.editor.has_unsaved_changes()

    def _card_category(self, choice: Choice) -> None:
        if self._notes_tx_id is not None:
            self.assign(self._notes_tx_id, choice)

    def _card_rule(self) -> None:
        rows = self.selected_rows()
        if len(rows) == 1:
            self.create_rule_requested.emit(rows[0])

    # -- editing ---------------------------------------------------------------

    def _run(self, title: str, fn) -> bool:
        try:
            with self.app.session() as s:
                fn(s)
        except Exception as e:
            show_error(self, title, e)
            return False
        return True

    def _refresh_rows(self, ids: list[int]) -> None:
        with self.app.session() as s:
            for row in txs.list_transactions(s, txs.TxFilter(ids=ids)):
                self.model.replace_row(row)
        self._update_summary()
        self._show_details()
        self.data_changed.emit()

    @property
    def _days(self) -> int:
        return self.app.settings.app.matching.days

    def _involves_transfer(self, tx_ids: list[int]) -> bool:
        return any(
            any(s.transfer_account_id for s in r.splits)
            for r in self.model.rows()
            if r.id in tx_ids
        )

    def _after_change(self, tx_ids: list[int], transfer: bool) -> None:
        """Refresh after edits; transfers add/remove counter-bookings, so reload all."""
        if transfer or self._involves_transfer(tx_ids):
            self.refresh()
            self._show_details()
            self.data_changed.emit()
        else:
            self._refresh_rows(tx_ids)

    def assign(self, tx_id: int, choice: Choice) -> None:
        self.assign_many([tx_id], choice)

    def assign_many(self, ids: list[int], choice: Choice) -> None:
        """One session and one refresh for all ``ids`` (#58: one each took 106 ms per
        booking). A booking that cannot take the choice (a counter-booking, a transfer to
        its own account) is left out; the others are assigned, the reasons shown once."""
        skipped: list[str] = []

        def do(s):
            for tx_id in ids:
                try:  # SplitError comes before anything is changed
                    if choice.transfer_account_id is not None:
                        txs.mark_transfer(s, tx_id, choice.transfer_account_id, self._days)
                    else:
                        txs.set_category(s, tx_id, choice.category_id, self._days)
                except txs.SplitError as e:
                    if str(e) not in skipped:
                        skipped.append(str(e))

        had_transfer = self._involves_transfer(ids)
        if self._run(self.tr("Assign category"), do):
            self._after_change(ids, had_transfer or choice.transfer_account_id is not None)
        if skipped:
            show_error(self, self.tr("Assign category"), "\n".join(skipped))

    def _context_menu(self, pos) -> None:
        rows = self.selected_rows()
        if not rows:
            return
        ids = [r.id for r in rows]
        menu = QMenu(self)

        assign_menu = menu.addMenu(self.tr("Assign category"))
        self.fill_category_menu(assign_menu, lambda c: self.assign_many(ids, c))

        if any(r.is_counterpart for r in rows):
            # Counter-bookings follow their transfer; only show where they come from.
            src = next(r for r in rows if r.is_counterpart)
            info = menu.addAction(
                self.tr("Counter-booking of a transfer from '%1'").replace(
                    "%1", src.counterpart_of or ""
                )
            )
            info.setEnabled(False)
            if len(rows) == 1:
                menu.addAction(
                    self.tr("Go to original booking"),
                    lambda: self.show_booking(src.counterpart_of_id),
                )
            if all(r.is_counterpart for r in rows):
                menu.exec(self.table.viewport().mapToGlobal(pos))
                return
            ids = [r.id for r in rows if not r.is_counterpart]
            rows = [r for r in rows if not r.is_counterpart]
            menu.addSeparator()
        if len(rows) == 1:
            for cp_id, account in rows[0].counterparts:
                menu.addAction(
                    self.tr("Go to counter-booking in '%1'").replace("%1", account),
                    lambda i=cp_id: self.show_booking(i),
                )
        if len(rows) == 1 and rows[0].is_manual_entry:
            menu.addAction(self.tr("Edit booking…"), lambda: self.edit_booking(rows[0]))
        if rows and all(r.is_manual_entry for r in rows):
            menu.addAction(self.tr("Delete booking"), lambda: self.delete_bookings(ids))
        if len(rows) == 1:
            menu.addAction(self.tr("Split…"), lambda: self.split(rows[0]))
        if any(r.needs_review for r in rows):
            menu.addAction(self.tr("Accept suggestion"), lambda: self.accept(ids))
        menu.addAction(self.tr("Reset to automatic"), lambda: self.reset(ids))
        menu.addSeparator()
        if len(rows) == 1:
            menu.addAction(
                self.tr("Create rule from this booking…"),
                lambda: self.create_rule_requested.emit(rows[0]),
            )
        menu.exec(self.table.viewport().mapToGlobal(pos))

    def fill_category_menu(
        self,
        menu: QMenu,
        choose: Callable[[Choice], None],
        current: tuple[int | None, int | None] | None = None,
    ) -> None:
        """One submenu per group (and one for transfers to own accounts) with its
        categories; ``current`` (category id, transfer account id) is ticked."""
        submenu: QMenu = menu
        for ch in self.choices:
            if ch.is_group_header:
                submenu = menu.addMenu(ch.label.strip())
                continue
            act = QAction(ch.label.strip(), submenu)
            if current is not None and (ch.category_id, ch.transfer_account_id) == current:
                act.setCheckable(True)
                act.setChecked(True)
                font = submenu.menuAction().font()
                font.setBold(True)
                submenu.menuAction().setFont(font)  # the group of the current category
            act.triggered.connect(lambda _=False, c=ch: choose(c))
            submenu.addAction(act)

    def open_category_menu(self, index: QModelIndex) -> QMenu | None:
        """The category menu below a booking's category cell (double-click, Enter)."""
        if not index.isValid():
            return None
        r: TransactionRow | None = index.data(ROW_ROLE)
        if r is None:
            return None
        s = r.splits[0] if r.splits else None
        current = (s.category_id, s.transfer_account_id) if s else None
        menu = QMenu(self)
        self.fill_category_menu(menu, lambda c: self.assign(r.id, c), current)
        self.category_menu = menu  # for tests
        rect = self.table.visualRect(index)
        menu.exec(self.table.viewport().mapToGlobal(rect.bottomLeft()))
        return menu

    def split(self, row: TransactionRow) -> None:
        """The card's split editor for this booking (context menu "Split…", Ctrl+D)."""
        if self.card.row is None or self.card.row.id != row.id:
            self._select_id(row.id)
        if self.card.row is not None and self.card.row.id == row.id:
            self.card.start_split()

    # -- manual bookings -------------------------------------------------------

    def new_booking(self, account_id: int | None = None) -> None:
        default = account_id if account_id is not None else self.account.currentData()
        dlg = BookingDialog(self.app, self, default_account_id=default)
        if not dlg.exec() or dlg.entry is None:
            return
        new_id: list[int] = []
        if self._run(
            self.tr("New booking"),
            lambda s: new_id.append(txs.create_manual(s, self.app.settings, dlg.entry)),
        ):
            self.reload_filters()
            self._select_id(new_id[0])
            self.data_changed.emit()

    def edit_booking(self, row: TransactionRow) -> None:
        with self.app.session() as s:
            entry = txs.get_manual(s, row.id)
        dlg = BookingDialog(self.app, self, entry, is_split=row.is_split)
        if not dlg.exec() or dlg.entry is None:
            return
        if self._run(
            self.tr("Edit booking"),
            lambda s: txs.update_manual(s, self.app.settings, row.id, dlg.entry),
        ):
            self.reload_filters()
            self._select_id(row.id)
            self.data_changed.emit()

    def delete_bookings(self, ids: list[int]) -> None:
        answer = QMessageBox.question(
            self,
            self.tr("Delete booking"),
            self.tr(
                "Delete %n manual booking(s)? Counter-bookings of transfers are deleted too.",
                "",
                len(ids),
            ),
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        def do(s):
            for i in ids:
                txs.delete_manual(s, i)

        if self._run(self.tr("Delete booking"), do):
            self.refresh()
            self.data_changed.emit()

    def _select_id(self, tx_id: int) -> bool:
        for i in range(self.proxy.rowCount()):
            idx = self.proxy.index(i, 0)
            if idx.data(ROW_ROLE).id == tx_id:
                self.table.selectRow(i)
                self.table.scrollTo(idx)
                return True
        return False

    def show_booking(self, tx_id: int | None) -> None:
        """Select a booking; filters that hide it are widened."""
        if tx_id is None:
            return
        if not self._select_id(tx_id):
            with self.app.session() as s:
                day = txs.get_row(s, tx_id).booking_date
            self.show_selection(
                txs.ALL,
                min(day, from_qdate(self.date_from.date())),
                max(day, from_qdate(self.date_to.date())),
            )
            self._select_id(tx_id)
        self.table.setFocus()

    def _full_range(self) -> tuple[date, date] | None:
        """First booking to today (or the last booking, if later)."""
        with self.app.session() as s:
            first, last = booking_date_range(s)
        if first is None:
            return None
        return first, max(last or date.today(), date.today())

    def start_review(self) -> None:
        """All suggestions of the classifier, over the whole period."""
        with self.app.session() as s:
            first, last = booking_date_range(s)
        self.show_selection(txs.REVIEW, first, max(last or date.today(), date.today()))
        if self.proxy.rowCount():
            self.table.selectRow(0)
        self.table.setFocus()

    def accept_selected(self) -> None:
        rows = [r for r in self.selected_rows() if r.needs_review]
        if not rows:
            return
        position = min(self.table.selectionModel().selectedRows(), key=lambda i: i.row()).row()
        self.accept([r.id for r in rows])
        if self.current_filter().category == txs.REVIEW:
            # accepted rows leave the list: continue with the next one
            self.refresh()
            if self.proxy.rowCount():
                self.table.selectRow(min(position, self.proxy.rowCount() - 1))

    def _return_pressed(self) -> None:
        """Enter: accept a suggestion while reviewing, else change the category."""
        if any(r.needs_review for r in self.selected_rows()):
            self.accept_selected()
        else:
            self.edit_category()

    def edit_category(self) -> None:
        current = self.table.currentIndex()
        if current.isValid():
            self.table.edit(self.proxy.index(current.row(), Col.CATEGORY))

    def _delete_key(self) -> None:
        rows = self.selected_rows()
        if rows and all(r.is_manual_entry for r in rows):
            self.delete_bookings([r.id for r in rows])

    def accept_all_shown(self) -> None:
        ids = [r.id for r in self.model.rows() if r.needs_review]
        if not ids:
            return
        answer = QMessageBox.question(
            self,
            self.tr("Accept suggestion"),
            self.tr("Accept the suggested categories of all %1 bookings shown?").replace(
                "%1", str(len(ids))
            ),
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.accept(ids)
            self.refresh()

    def accept(self, ids: list[int]) -> None:
        def do(s):
            for i in ids:
                txs.accept_suggestion(s, i)

        if self._run(self.tr("Accept suggestion"), do):
            self._refresh_rows(ids)

    def reset(self, ids: list[int]) -> None:
        if self._run(
            self.tr("Reset to automatic"),
            lambda s: txs.reset_to_automatic(s, self.app.settings, ids),
        ):
            self._after_change(ids, transfer=True)  # rules may create or remove transfers
