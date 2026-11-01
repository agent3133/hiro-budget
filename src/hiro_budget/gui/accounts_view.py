"""Accounts tab: balances per account and an editor for own_accounts in config.yaml."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSplitter,
    QStyledItemDelegate,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from hiro_budget.app import App
from hiro_budget.config import BUDGET_KINDS, OwnAccount, PatternMatch
from hiro_budget.gui import theme
from hiro_budget.gui.common import (
    accent_color,
    amount_color,
    fmt_date,
    from_qdate,
    money,
    show_error,
    to_qdate,
    warning_color,
)
from hiro_budget.gui.holdings_dialog import HoldingsDialog
from hiro_budget.gui.navigation import ICON_DELETE, ICON_DOWN, ICON_UP
from hiro_budget.gui.widgets import (
    DetailCard,
    action_bar,
    icon_button,
    page_title,
    paint_chip,
    primary,
)
from hiro_budget.services import accounts as acc_service
from hiro_budget.services import config_edit
from hiro_budget.services.accounts import AccountBalance, Position, Positions

KINDS = ["giro", "savings", "cash", "credit_card", "wallet", "depot", "loan"]
# summary cards in the header: which account kinds each one adds up
CARD_KINDS = {
    "liquid": ("giro", "savings", "cash", "credit_card", "wallet"),
    "depot": ("depot",),
    "loan": ("loan",),
}
COL_NAME, COL_TYPE, COL_BALANCE, COL_LAST, COL_STATEMENT, COL_COUNT = range(6)
ACCOUNT_ROLE = Qt.ItemDataRole.UserRole + 1  # account name of account rows
# index of an account row's draft in AccountsView.drafts (the saved order): the rows can be
# sorted by a column, so a row number is not a draft index
DRAFT_ROLE = Qt.ItemDataRole.UserRole + 2
SORT_ROLE = Qt.ItemDataRole.UserRole + 3  # value to sort a column by (numbers, dates)
RIGHT = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
Drop = QAbstractItemView.DropIndicatorPosition


class AccountItem(QTreeWidgetItem):
    """Sorts by SORT_ROLE where set (amounts, dates, counts), else by text; empty cells
    first."""

    def __lt__(self, other: QTreeWidgetItem) -> bool:
        col = self.treeWidget().sortColumn() if self.treeWidget() else 0

        def key(item: QTreeWidgetItem):
            value = item.data(col, SORT_ROLE)
            if value is None:
                value = item.text(col).casefold()
            return (value != "" and value is not None, value)

        return key(self) < key(other)


class AccountTree(QTreeWidget):
    """Drag and drop of account rows: the view reorders its drafts and fills the table
    again, so Qt must not move the rows itself."""

    #: (dragged item, item under the cursor, drop indicator position)
    dropped = Signal(QTreeWidgetItem, QTreeWidgetItem, Drop)

    def dropEvent(self, event) -> None:
        item = self.currentItem()
        target = self.itemAt(event.position().toPoint())
        if item is not None and target is not None and target is not item:
            self.dropped.emit(item, target, self.dropIndicatorPosition())
        event.setDropAction(Qt.DropAction.IgnoreAction)
        event.accept()


@dataclass
class AccountDraft:
    name: str
    kind: str = "cash"
    institution: str = ""
    iban: str = ""
    email: str = ""
    counterparty: str = ""
    purpose: str = ""
    opening_balance: float | None = None
    opening_date: date | None = None
    on_budget: bool | None = None  # None: default by kind
    transfer_category: str = ""
    original_name: str | None = None  # None: new account
    in_config: bool = True
    balance: AccountBalance | None = field(default=None, repr=False)
    positions: Positions | None = field(default=None, repr=False)  # securities accounts

    @classmethod
    def from_config(cls, a: OwnAccount) -> AccountDraft:
        return cls(
            name=a.name,
            kind=a.kind,
            institution=a.institution or "",
            iban=a.iban or "",
            email=a.email or "",
            counterparty=(a.match.counterparty if a.match else None) or "",
            purpose=(a.match.purpose if a.match else None) or "",
            opening_balance=a.opening_balance,
            opening_date=a.opening_date,
            on_budget=a.on_budget,
            transfer_category=a.transfer_category or "",
            original_name=a.name,
        )

    @property
    def is_on_budget(self) -> bool:
        return self.on_budget if self.on_budget is not None else self.kind in BUDGET_KINDS

    def to_config(self) -> OwnAccount:
        match = None
        if self.counterparty.strip() or self.purpose.strip():
            match = PatternMatch(
                counterparty=self.counterparty.strip() or None,
                purpose=self.purpose.strip() or None,
            )
        return OwnAccount(
            name=self.name.strip(),
            kind=self.kind,  # type: ignore[arg-type]
            institution=self.institution.strip() or None,
            iban=self.iban.strip() or None,
            email=self.email.strip() or None,
            match=match,
            opening_balance=self.opening_balance,
            opening_date=self.opening_date,
            on_budget=self.on_budget,
            transfer_category=self.transfer_category or None,
        )


class TypeChipDelegate(QStyledItemDelegate):
    """Obsidian: the type of an account as a chip (positions keep their ISIN as text)."""

    def paint(self, painter, option, index):
        t = theme.tokens()
        text = index.data() or ""
        if t is None or index.parent().isValid() or not text:
            super().paint(painter, option, index)
            return
        paint_chip(painter, option, text, t["panel"], t["muted"])

    def sizeHint(self, option, index):
        size = super().sizeHint(option, index)
        if theme.tokens() is not None:  # room around the chips
            size.setHeight(size.height() + 8)
            if not index.parent().isValid():  # the chip's padding and margins
                size.setWidth(size.width() + 30)
        return size


class SummaryCard(QFrame):
    """A sum in the header: small title, large amount."""

    def __init__(self, title: str, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("summaryCard")
        self.highlight = False  # the total: in the accent colour
        self.cents = 0
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 10, 16, 12)
        layout.setSpacing(2)
        self.title = QLabel(title)
        self.amount = QLabel()
        self.amount.setObjectName("amount")
        font = self.amount.font()
        font.setPointSize(font.pointSize() + 7)
        font.setBold(True)
        self.amount.setFont(font)
        layout.addWidget(self.title)
        layout.addSpacing(4)
        layout.addWidget(self.amount)

    def set_amount(self, cents: int, accounts: list[str]) -> None:
        self.amount.setText(money(cents))
        self.setToolTip("\n".join(accounts))
        self.cents = cents
        self.restyle()

    def restyle(self) -> None:
        t = theme.tokens()
        negative = self.cents < 0
        if t is not None:
            background = t["accent_soft"] if self.highlight else t["panel"]
            title = t["heading"] if self.highlight else t["muted"]
            ink = t["heading"] if self.highlight else t["neg_text"] if negative else t["text"]
        else:
            accent = accent_color()
            background = (
                f"rgba({accent.red()}, {accent.green()}, {accent.blue()}, 0.16)"
                if self.highlight
                else "rgba(128, 128, 128, 0.10)"
            )
            title = "palette(text)"
            ink = amount_color(-1).name() if negative else "palette(text)"
        self.setStyleSheet(
            f"QFrame#summaryCard {{ background: {background}; border-radius: 12px; }}"
            f"QFrame#summaryCard QLabel {{ background: transparent; color: {title}; }}"
            f"QFrame#summaryCard QLabel#amount {{ color: {ink}; }}"
        )


def saved_balance_list(app: App) -> list[tuple[str, int]]:
    """The accounts with their balances as saved, in the order of the accounts page (the
    sidebar, before that page was opened)."""
    with app.session() as s:
        balances = {b.name: b.balance_cents for b in acc_service.account_balances(s)}
    names = [a.name for a in app.settings.app.own_accounts]
    names += [n for n in balances if n not in names]
    return [(n, balances[n]) for n in names if n in balances]


class AccountsView(QWidget):
    #: emitted after accounts were saved (other views reload account lists)
    changed = Signal()
    #: emitted to enter a manual booking for the given account id
    new_booking_requested = Signal(int)
    #: balances were (re)loaded: [(account name, cents)], for the sidebar
    balances_changed = Signal(list)
    #: unsaved changes appeared (True) or were saved / discarded (False)
    dirty_changed = Signal(bool)

    def __init__(self, app: App, parent=None) -> None:
        super().__init__(parent)
        self.app = app
        self.drafts: list[AccountDraft] = []
        self.removed: list[str] = []
        self._loading = False
        self.kind_labels = {
            "giro": self.tr("Checking account"),
            "savings": self.tr("Savings account"),
            "cash": self.tr("Cash"),
            "credit_card": self.tr("Credit card"),
            "wallet": self.tr("Payment service (PayPal)"),
            "depot": self.tr("Securities account"),
            "loan": self.tr("Loan"),
        }

        # -- balance table (securities accounts expand to their positions) --
        self.table = AccountTree()
        self.table.setColumnCount(6)
        self.table.setHeaderLabels(
            [
                self.tr("Account"),
                self.tr("Type"),
                self.tr("Balance"),
                self.tr("Last booking"),
                self.tr("Latest statement"),
                self.tr("Bookings"),
            ]
        )
        # only the names take the room left; the other columns as wide as their content
        self.table.header().setStretchLastSection(False)
        self.table.header().setSectionResizeMode(COL_NAME, QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setUniformRowHeights(True)
        self.table.setItemDelegateForColumn(COL_TYPE, TypeChipDelegate(self.table))
        self.table.currentItemChanged.connect(lambda *_: self._show_draft())
        # accounts whose positions are hidden
        self.collapsed: set[str] = set(app.ui_state.get("accounts.collapsed", []))
        self.table.itemCollapsed.connect(lambda i: self._account_toggled(i, False))
        self.table.itemExpanded.connect(lambda i: self._account_toggled(i, True))
        # order: drag and drop or up/down change the saved order; a click on a column
        # header only sorts the view (ascending, descending, back to the saved order)
        self._sort: tuple[int, Qt.SortOrder] | None = None
        self._saved_order: list[str] = []  # account names as saved (sidebar order)
        header = self.table.header()
        header.setSectionsClickable(True)
        header.setSortIndicatorShown(False)
        header.sectionClicked.connect(self._header_clicked)
        self.table.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.table.setDropIndicatorShown(True)
        self.table.dropped.connect(self._dropped)
        for key, delta in (("Alt+Up", -1), ("Alt+Down", 1)):
            shortcut = QShortcut(QKeySequence(key), self.table)
            shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            shortcut.activated.connect(lambda delta=delta: self._move(delta))
        # -- header: page title and the sums per kind of account --
        self.title = page_title(self.tr("Accounts"))
        self.cards = {
            "liquid": SummaryCard(self.tr("Liquid funds")),
            "depot": SummaryCard(self.tr("Securities")),
            "loan": SummaryCard(self.tr("Loans")),
            "total": SummaryCard(self.tr("Total of all accounts")),
        }
        self.cards["total"].highlight = True
        cards_row = QHBoxLayout()
        cards_row.setSpacing(12)
        for card in self.cards.values():
            cards_row.addWidget(card, 1)
        self.setFocusProxy(self.table)  # keyboard focus when the page is shown

        add = QPushButton(self.tr("New account"))
        add.clicked.connect(self._add)
        self.remove_button = icon_button(ICON_DELETE, "✕", self.tr("Remove account"), self._remove)
        self.book_button = QPushButton(self.tr("New booking in this account…"))
        self.book_button.clicked.connect(self._new_booking)
        self.holdings_button = QPushButton(self.tr("Holdings…"))
        self.holdings_button.clicked.connect(self._show_holdings)
        self.up_button = icon_button(
            ICON_UP, "↑", self.tr("Move up (Alt+↑)"), lambda: self._move(-1)
        )
        self.down_button = icon_button(
            ICON_DOWN, "↓", self.tr("Move down (Alt+↓)"), lambda: self._move(1)
        )
        left_buttons = action_bar(
            [add, self.book_button, self.holdings_button],
            [self.up_button, self.down_button, self.remove_button],
        )

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.addWidget(self.table, 1)
        left_layout.addLayout(left_buttons)

        # -- editor --
        self.name = QLineEdit()
        self.kind = QComboBox()
        for k in KINDS:
            self.kind.addItem(self.kind_labels[k], k)
        self.institution = QLineEdit()
        self.iban = QLineEdit()
        self.email = QLineEdit(placeholderText=self.tr("PayPal: the account's e-mail address"))
        self.email.setToolTip(
            self.tr("CSV exports of a payment service are assigned to the account by it.")
        )
        self.on_budget = QCheckBox(self.tr("Money is assigned to categories"))
        self.on_budget.setToolTip(
            self.tr(
                "Money in budget accounts must be assigned to categories. Loans and "
                "securities accounts are usually tracking accounts (balance only)."
            )
        )
        self.has_opening = QCheckBox()  # in the row "Opening balance", before the amount
        self.has_opening.setToolTip(self.tr("Use an opening balance"))
        self.has_opening.setAccessibleName(self.tr("Opening balance"))
        self.opening_balance = QDoubleSpinBox()
        self.opening_balance.setRange(-100_000_000, 100_000_000)
        self.opening_balance.setDecimals(2)
        self.opening_balance.setSuffix(" €")
        self.opening_balance.setGroupSeparatorShown(True)
        self.opening_date = QDateEdit(calendarPopup=True, displayFormat="dd.MM.yyyy")
        opening_row = QHBoxLayout()
        opening_row.setSpacing(8)
        opening_row.addWidget(self.has_opening)
        opening_row.addWidget(self.opening_balance, 1)
        opening_row.addWidget(QLabel(self.tr("on")))
        opening_row.addWidget(self.opening_date)
        self.counterparty = QLineEdit(placeholderText=self.tr("Regex, e.g. (?i)extra-konto"))
        self.purpose = QLineEdit(placeholderText=self.tr("Regex on purpose text"))
        self.transfer_category = QComboBox()
        self.transfer_category.setToolTip(
            self.tr(
                "Tracking accounts only: budget category for money moved here from a budget "
                "account, e.g. securities purchases. Without it, such transfers are "
                "uncategorized outflow."
            )
        )

        form = QFormLayout()
        form.setVerticalSpacing(10)
        form.setHorizontalSpacing(16)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        form.addRow(self.tr("Name"), self.name)
        form.addRow(self.tr("Type"), self.kind)
        form.addRow(self.tr("Institution"), self.institution)
        form.addRow(self.tr("IBAN / No."), self.iban)
        form.addRow(self.tr("E-mail"), self.email)
        form.addRow(self.tr("Budget"), self.on_budget)
        form.addRow(self.tr("Opening balance"), opening_row)
        transfer_box = QGroupBox(self.tr("Recognize transfers to this account"))
        transfer_form = QFormLayout(transfer_box)
        transfer_form.addRow(self.tr("Counterparty"), self.counterparty)
        transfer_form.addRow(self.tr("Purpose"), self.purpose)
        transfer_form.addRow(self.tr("Budget category"), self.transfer_category)
        self.info = QLabel()
        self.info.setWordWrap(True)

        self.save_button = primary(QPushButton(self.tr("Save accounts")))
        self.save_button.clicked.connect(self.save)
        self.revert_button = QPushButton(self.tr("Revert"))
        self.revert_button.clicked.connect(self.load)
        save_row = QHBoxLayout()
        save_row.addStretch(1)
        save_row.addWidget(self.revert_button)
        save_row.addWidget(self.save_button)

        editor = self.editor = DetailCard(self.tr("Account settings"))
        # the fields scroll in a short window (laptop), the buttons stay visible
        fields = QWidget()
        fields.setAutoFillBackground(False)
        fields_layout = QVBoxLayout(fields)
        fields_layout.setContentsMargins(0, 0, 0, 0)
        fields_layout.addLayout(form)
        fields_layout.addSpacing(10)
        fields_layout.addWidget(transfer_box)
        fields_layout.addWidget(self.info)
        fields_layout.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidget(fields)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        fields.setObjectName("accountFields")
        scroll.setObjectName("accountScroll")
        scroll.setStyleSheet(  # the card's colour behind the fields
            "QScrollArea#accountScroll, QWidget#accountFields { background: transparent; }"
        )
        scroll.viewport().setAutoFillBackground(False)
        editor_layout = editor.body
        editor_layout.addWidget(scroll, 1)
        editor_layout.addLayout(save_row)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(left)
        splitter.addWidget(editor)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        layout = QVBoxLayout(self)
        layout.addWidget(self.title)
        layout.addSpacing(4)
        layout.addLayout(cards_row)
        layout.addSpacing(4)
        layout.addWidget(splitter, 1)

        for edit in (
            self.name,
            self.institution,
            self.iban,
            self.email,
            self.counterparty,
            self.purpose,
        ):
            edit.textEdited.connect(self._form_changed)
        self.kind.currentIndexChanged.connect(self._form_changed)
        self.transfer_category.currentIndexChanged.connect(self._form_changed)
        self.has_opening.toggled.connect(self._form_changed)
        self.on_budget.toggled.connect(self._budget_toggled)
        self.opening_balance.valueChanged.connect(self._form_changed)
        self.opening_date.dateChanged.connect(self._form_changed)
        self.load()

    # -- loading ---------------------------------------------------------------

    def load(self) -> None:
        """Reload accounts from config and balances from the database (discards edits)."""
        self._loading = True
        self.transfer_category.clear()
        self.transfer_category.addItem(self.tr("(none)"), "")
        for g in self.app.settings.categories.groups:
            if not g.income:
                for c in g.categories:
                    self.transfer_category.addItem(f"{g.name}: {c.name}", f"{g.key}.{c.key}")
        self._loading = False
        balances, positions = self._load_balances()
        drafts = [AccountDraft.from_config(a) for a in self.app.settings.app.own_accounts]
        known = {d.name for d in drafts}
        for name, b in balances.items():
            if name not in known:  # created automatically by an import
                drafts.append(
                    AccountDraft(
                        name=name,
                        kind=b.kind.value,
                        institution=b.institution or "",
                        iban=b.iban or "",
                        original_name=name,
                        in_config=False,
                    )
                )
        for d in drafts:
            d.balance = balances.get(d.original_name or "")
            d.positions = positions.get(d.original_name or "")
        self.drafts = drafts
        self._saved_order = [d.name for d in drafts]
        self.removed = []
        self._fill_table(0)
        self._set_dirty(False)

    def reload_balances(self) -> None:
        """Refresh balances after bookings changed, keeping unsaved edits."""
        balances, positions = self._load_balances()
        for d in self.drafts:
            d.balance = balances.get(d.original_name or "")
            d.positions = positions.get(d.original_name or "")
        self._fill_table(self._current_index())

    def _load_balances(self) -> tuple[dict[str, AccountBalance], dict[str, Positions]]:
        with self.app.session() as s:
            balances = {b.name: b for b in acc_service.account_balances(s)}
            positions = {
                b.name: p
                for b in balances.values()
                if b.anchor == "valuation"
                for p in [acc_service.positions(s, b.account_id)]
                if p is not None
            }
        return balances, positions

    def _fill_table(self, select: int) -> None:
        self._loading = True
        self.table.clear()
        total = 0
        for index, d in enumerate(self.drafts):
            b = d.balance
            name = d.name + ("" if d.in_config else self.tr(" (not in configuration)"))
            statement = (
                f"{money(b.statement_balance_cents)} ({fmt_date(b.statement_date)})"
                if b and b.statement_balance_cents is not None
                else ""
            )
            item = AccountItem(
                [
                    name,
                    self.kind_labels.get(d.kind, d.kind),
                    money(b.balance_cents) if b else "",
                    fmt_date(b.last_booking) if b else "",
                    statement,
                    str(b.booking_count) if b else "0",
                ]
            )
            item.setData(COL_NAME, ACCOUNT_ROLE, d.original_name)
            item.setData(COL_NAME, DRAFT_ROLE, index)
            # rows can be dragged between rows, nothing can be dropped onto them
            item.setFlags(
                (item.flags() | Qt.ItemFlag.ItemIsDragEnabled) & ~Qt.ItemFlag.ItemIsDropEnabled
            )
            if b:
                item.setData(COL_BALANCE, SORT_ROLE, b.balance_cents)
                item.setData(
                    COL_LAST, SORT_ROLE, b.last_booking.toordinal() if b.last_booking else ""
                )
                item.setData(
                    COL_STATEMENT,
                    SORT_ROLE,
                    b.statement_date.toordinal() if b.statement_date else "",
                )
                item.setData(COL_COUNT, SORT_ROLE, b.booking_count)
            for col in (COL_BALANCE, COL_STATEMENT, COL_COUNT):
                item.setTextAlignment(col, RIGHT)
            if b:
                total += b.balance_cents
                self._balance_cell(item, b)
            self.table.addTopLevelItem(item)
            if d.positions is not None:
                for p in d.positions.positions:
                    child = self._position_item(p, d.positions)
                    child.setFlags(
                        child.flags()
                        & ~Qt.ItemFlag.ItemIsDragEnabled
                        & ~Qt.ItemFlag.ItemIsDropEnabled
                    )
                    item.addChild(child)
                item.setExpanded(d.original_name not in self.collapsed)
        header = self.table.header()
        if self._sort is not None:
            self.table.sortItems(*self._sort)
            header.setSortIndicator(*self._sort)
        header.setSortIndicatorShown(self._sort is not None)
        for col in range(1, self.table.columnCount()):
            self.table.resizeColumnToContents(col)
        self._fill_cards(total)
        self.balances_changed.emit(self.balance_list())
        self._loading = False
        if self.drafts:
            self.table.setCurrentItem(self._item_of(max(0, min(select, len(self.drafts) - 1))))
        self._show_draft()

    def _item_of(self, index: int) -> QTreeWidgetItem | None:
        """The table row of ``drafts[index]`` (wherever a column sort put it)."""
        for i in range(self.table.topLevelItemCount()):
            item = self.table.topLevelItem(i)
            if item.data(COL_NAME, DRAFT_ROLE) == index:
                return item
        return None

    # -- order -----------------------------------------------------------------

    def _header_clicked(self, column: int) -> None:
        """Sort the view by a column: ascending, descending, then the saved order again."""
        asc, desc = Qt.SortOrder.AscendingOrder, Qt.SortOrder.DescendingOrder
        if self._sort is None or self._sort[0] != column:
            self._sort = (column, asc)
        elif self._sort[1] == asc:
            self._sort = (column, desc)
        else:
            self._sort = None
        self._fill_table(self._current_index())

    def _move(self, delta: int) -> None:
        """Move the selected account up (-1) or down (1) in the saved order."""
        i = self._current_index()
        j = i + delta
        if self._sort is not None or i < 0 or not 0 <= j < len(self.drafts):
            return
        self.drafts[i], self.drafts[j] = self.drafts[j], self.drafts[i]
        self._fill_table(j)
        self._set_dirty(True)

    def _dropped(self, item: QTreeWidgetItem, target: QTreeWidgetItem, position: Drop) -> None:
        """An account dropped above or below another one (on a position: its account)."""
        if self._sort is not None:
            return
        while target.parent() is not None:
            target = target.parent()
        source = item.data(COL_NAME, DRAFT_ROLE)
        dest = target.data(COL_NAME, DRAFT_ROLE)
        if source is None or dest is None or source == dest:
            return
        if position == Drop.AboveItem:
            index = dest
        elif position == Drop.BelowItem:
            index = dest + 1
        else:  # on the row: before it when moving up, after it when moving down
            index = dest if source > dest else dest + 1
        draft = self.drafts.pop(source)
        if source < index:
            index -= 1  # the draft itself left a gap
        if index == source:
            self.drafts.insert(source, draft)
            return
        self.drafts.insert(index, draft)
        self._fill_table(index)
        self._set_dirty(True)

    def _update_order_controls(self) -> None:
        sorted_view = self._sort is not None
        i = self._current_index()
        self.table.setDragEnabled(not sorted_view)
        self.up_button.setEnabled(not sorted_view and i > 0)
        self.down_button.setEnabled(not sorted_view and 0 <= i < len(self.drafts) - 1)
        hint = (
            self.tr(
                "Sorted by a column: click its header until the sort is gone to change the order"
            )
            if sorted_view
            else ""
        )
        self.up_button.setToolTip(hint or self.tr("Move up (Alt+↑)"))
        self.down_button.setToolTip(hint or self.tr("Move down (Alt+↓)"))

    def _fill_cards(self, total: int) -> None:
        """Sums per kind of account; a card without such accounts is hidden."""
        with_balance = [d for d in self.drafts if d.balance]
        for key, kinds in CARD_KINDS.items():
            found = [d for d in with_balance if d.kind in kinds]
            self.cards[key].set_amount(
                sum(d.balance.balance_cents for d in found), [d.name for d in found]
            )
            self.cards[key].setVisible(bool(found))
        self.cards["total"].set_amount(total, [d.name for d in with_balance])

    def _account_toggled(self, item: QTreeWidgetItem, expanded: bool) -> None:
        name = item.data(COL_NAME, ACCOUNT_ROLE)
        if name is None or self._loading:
            return
        (self.collapsed.discard if expanded else self.collapsed.add)(name)
        self.app.ui_state.set("accounts.collapsed", sorted(self.collapsed))

    def _balance_cell(self, item: QTreeWidgetItem, b: AccountBalance) -> None:
        item.setForeground(COL_BALANCE, amount_color(b.balance_cents))
        if b.anchor == "none":
            tip = self.tr("No opening balance: sum of all bookings of this account.")
        elif b.anchor == "valuation":
            tip = self.tr(
                "Balance (securities: value) of the latest statement (%1) plus later bookings."
            ).replace("%1", fmt_date(b.statement_date))
        elif b.anchor == "statement":
            tip = self.tr(
                "Opening balance of the first imported statement (%1) plus bookings."
            ).replace("%1", fmt_date(b.anchor_date))
        else:
            tip = self.tr("Configured opening balance (%1) plus bookings.").replace(
                "%1", fmt_date(b.anchor_date)
            )
        gaps = [g for g in b.gaps if b.anchor_date is None or g.before.period_start > b.anchor_date]
        if gaps:
            item.setText(COL_BALANCE, "⚠ " + item.text(COL_BALANCE))
            item.setForeground(COL_BALANCE, warning_color())
            lines = [
                self.tr("Statements missing for %1 – %2: bookings of %3 are not imported.")
                .replace("%1", fmt_date(g.after.period_end + timedelta(days=1)))
                .replace("%2", fmt_date(g.before.period_start - timedelta(days=1)))
                .replace("%3", money(g.difference_cents))
                for g in gaps
            ]
            lines.append(self.tr("The balance is wrong until they are imported."))
            tip = "\n".join([tip, "", *lines])
        item.setToolTip(COL_BALANCE, tip)

    def _position_item(self, p: Position, positions: Positions) -> QTreeWidgetItem:
        """Sub-row of a securities account: one security (or the rest without ISIN)."""
        name = p.name if p.isin is not None else self.tr("Other")
        value = money(p.value_cents) + (" *" if p.trades else "")
        last = fmt_date(p.trades[-1].date) if p.trades else ""
        item = QTreeWidgetItem([name, p.isin or "", value, last, "", ""])
        item.setTextAlignment(COL_BALANCE, RIGHT)
        item.setFlags(Qt.ItemFlag.ItemIsEnabled)  # not selectable: the account row is
        lines = [
            self.tr("Depot statement %1: %2")
            .replace("%1", fmt_date(positions.statement_date))
            .replace("%2", money(p.statement_cents))
        ]
        for t in p.trades:
            text = self.tr("Bought %1: %2") if t.amount_cents > 0 else self.tr("Sold %1: %2")
            lines.append(
                text.replace("%1", fmt_date(t.date)).replace("%2", money(abs(t.amount_cents)))
            )
        if p.trades:
            lines += [
                "",
                self.tr(
                    "* At cost: price changes since the statement are not included. "
                    "The next depot statement brings the market value."
                ),
            ]
        if p.isin is None:
            lines.append(self.tr("Value or bookings without an ISIN."))
        item.setToolTip(COL_BALANCE, "\n".join(lines))
        item.setToolTip(COL_NAME, "\n".join(lines))
        return item

    # -- editing ---------------------------------------------------------------

    def _current(self) -> AccountDraft | None:
        row = self._current_index()
        return self.drafts[row] if 0 <= row < len(self.drafts) else None

    def _current_index(self) -> int:
        """Index of the selected account (a position row selects its account)."""
        item = self.table.currentItem()
        if item is None:
            return -1
        while item.parent() is not None:
            item = item.parent()
        index = item.data(COL_NAME, DRAFT_ROLE)
        return index if index is not None else -1

    def balance_list(self) -> list[tuple[str, int]]:
        """For the sidebar: in the saved order (a new order counts once it is saved)."""
        rank = {name: i for i, name in enumerate(self._saved_order)}
        ordered = sorted(self.drafts, key=lambda d: rank.get(d.original_name or "", len(rank)))
        return [(d.name, d.balance.balance_cents) for d in ordered if d.balance]

    def nav_state(self) -> str | None:
        """The selected account, for going back here (history)."""
        d = self._current()
        return d.name if d else None

    def restore_nav_state(self, name: str | None) -> None:
        if name in [d.name for d in self.drafts]:
            self.select_account(name)

    def select_account(self, name: str) -> None:
        index = [d.name for d in self.drafts].index(name)
        self.table.setCurrentItem(self._item_of(index))

    def _set_dirty(self, dirty: bool) -> None:
        self.save_button.setEnabled(dirty)
        self.revert_button.setEnabled(dirty)
        self.dirty_changed.emit(dirty)

    def has_unsaved_changes(self) -> bool:
        return self.save_button.isEnabled()

    def _show_draft(self) -> None:
        if self._loading:
            return
        self._update_order_controls()
        d = self._current()
        self.editor.set_heading(d.name if d else "")
        self._loading = True
        enabled = d is not None
        for w in (
            self.name,
            self.kind,
            self.institution,
            self.iban,
            self.email,
            self.on_budget,
            self.has_opening,
            self.counterparty,
            self.purpose,
        ):
            w.setEnabled(enabled)
        self.transfer_category.setEnabled(enabled and not d.is_on_budget)
        self.holdings_button.setEnabled(
            enabled
            and d.kind == "depot"
            and d.balance is not None
            and d.balance.anchor == "valuation"
        )
        self.remove_button.setEnabled(enabled)
        self.book_button.setEnabled(enabled and d.original_name is not None)
        if d is not None:
            self.name.setText(d.name)
            self.kind.setCurrentIndex(max(0, self.kind.findData(d.kind)))
            self.institution.setText(d.institution)
            self.iban.setText(d.iban)
            self.email.setText(d.email)
            self.on_budget.setChecked(d.is_on_budget)
            self.counterparty.setText(d.counterparty)
            self.purpose.setText(d.purpose)
            self.transfer_category.setCurrentIndex(
                max(0, self.transfer_category.findData(d.transfer_category))
            )
            self.has_opening.setChecked(d.opening_balance is not None)
            self.opening_balance.setValue(d.opening_balance or 0.0)
            self.opening_date.setDate(to_qdate(d.opening_date or date(date.today().year, 1, 1)))
            info = []
            if not d.in_config:
                info.append(
                    self.tr(
                        "This account was created by an import. Saving adds it to the "
                        "configuration."
                    )
                )
            if d.balance and d.balance.manual_count:
                info.append(
                    self.tr("%1 manual bookings or transfer counter-bookings.").replace(
                        "%1", str(d.balance.manual_count)
                    )
                )
            self.info.setText("\n".join(info))
        self.opening_balance.setEnabled(enabled and self.has_opening.isChecked())
        self.opening_date.setEnabled(enabled and self.has_opening.isChecked())
        self._loading = False

    def _form_changed(self) -> None:
        self.opening_balance.setEnabled(self.has_opening.isChecked())
        self.opening_date.setEnabled(self.has_opening.isChecked())
        if self._loading:
            return
        d = self._current()
        if d is None:
            return
        d.name = self.name.text()
        self.editor.set_heading(d.name)
        d.kind = self.kind.currentData()
        self.on_budget.blockSignals(True)
        self.on_budget.setChecked(d.is_on_budget)  # the default follows the kind
        self.on_budget.blockSignals(False)
        d.institution = self.institution.text()
        d.iban = self.iban.text()
        d.email = self.email.text()
        d.counterparty = self.counterparty.text()
        d.purpose = self.purpose.text()
        d.transfer_category = self.transfer_category.currentData() or ""
        self.transfer_category.setEnabled(not d.is_on_budget)
        if self.has_opening.isChecked():
            d.opening_balance = round(self.opening_balance.value(), 2)
            d.opening_date = from_qdate(self.opening_date.date())
        else:
            d.opening_balance = d.opening_date = None
        item = self._item_of(self._current_index())
        item.setText(COL_NAME, d.name)
        item.setText(COL_TYPE, self.kind_labels.get(d.kind, d.kind))
        self._set_dirty(True)

    def _budget_toggled(self, checked: bool) -> None:
        if self._loading:
            return
        d = self._current()
        if d is None:
            return
        # Only store an explicit value when it differs from the default for the kind.
        d.on_budget = None if checked == (d.kind in BUDGET_KINDS) else checked
        self.transfer_category.setEnabled(not d.is_on_budget)
        self._set_dirty(True)

    def _add(self) -> None:
        taken = {d.name for d in self.drafts}
        name, i = self.tr("Cash"), 2
        while name in taken:
            name, i = f"{self.tr('Cash')} {i}", i + 1
        self.drafts.append(
            AccountDraft(name=name, kind="cash", opening_balance=0.0, opening_date=date.today())
        )
        self._fill_table(len(self.drafts) - 1)
        self._set_dirty(True)
        self.name.setFocus()
        self.name.selectAll()

    def _remove(self) -> None:
        d = self._current()
        if d is None:
            return
        if d.balance and d.balance.booking_count:
            show_error(
                self,
                self.tr("Remove account"),
                self.tr(
                    "'%1' has %2 bookings and cannot be removed. Delete its imports and "
                    "manual bookings first."
                )
                .replace("%1", d.name)
                .replace("%2", str(d.balance.booking_count)),
            )
            return
        if d.original_name is not None:
            self.removed.append(d.original_name)
        self.drafts.remove(d)
        self._fill_table(self._current_index())
        self._set_dirty(True)

    def _new_booking(self) -> None:
        d = self._current()
        if d is not None and d.balance is not None:
            self.new_booking_requested.emit(d.balance.account_id)

    def _show_holdings(self) -> None:
        d = self._current()
        if d is not None and d.balance is not None:
            HoldingsDialog(self.app, d.balance.account_id, d.name, self).exec()

    def save(self) -> None:
        try:
            accounts = [d.to_config() for d in self.drafts]
            renames = {
                d.original_name: d.name.strip()
                for d in self.drafts
                if d.original_name is not None and d.original_name != d.name.strip()
            }
            with self.app.session() as s:
                for old, new in renames.items():
                    acc_service.rename_account(s, old, new)
                for name in self.removed:
                    acc_service.delete_account(s, name)
                # Written last: if it fails, the database changes are rolled back.
                config_edit.save_accounts(self.app.settings, accounts, renames)
            self.app.reload_config()
        except Exception as e:
            show_error(self, self.tr("Save accounts"), e)
            return
        self.load()
        self.changed.emit()
