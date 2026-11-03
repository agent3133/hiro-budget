"""Budget tab: monthly envelope budget (YNAB style)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from PySide6.QtCore import QByteArray, QLocale, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QPainter
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from hiro_budget.app import App
from hiro_budget.gui import shortcuts, theme
from hiro_budget.gui.common import (
    accent_color,
    amount_color,
    group_colors,
    is_dark,
    money,
    show_error,
)
from hiro_budget.gui.move_money_dialog import MoveMoneyDialog
from hiro_budget.gui.navigation import (
    ICON_NEXT,
    ICON_PREVIOUS,
    ICON_REDO,
    ICON_UNDO,
    set_glyph,
)
from hiro_budget.gui.proposal_dialog import ProposalDialog
from hiro_budget.gui.target_dialog import TargetDialog
from hiro_budget.gui.widgets import action_bar, banner, caption, icon_button, page_title
from hiro_budget.money import apply_amount_input
from hiro_budget.services import budget, config_edit
from hiro_budget.services.budget import (
    BudgetMonth,
    CategoryMonth,
    Envelope,
    GroupMonth,
    month_add,
    month_of,
)

COL_NAME, COL_CARRY, COL_ASSIGNED, COL_ACTIVITY, COL_AVAILABLE, COL_TARGET = range(6)
COLUMNS = 6
MONEY_COLUMNS = (COL_CARRY, COL_ASSIGNED, COL_ACTIVITY, COL_AVAILABLE)
CAT_ROLE = Qt.ItemDataRole.UserRole + 1  # category id (category rows)
ENV_ROLE = Qt.ItemDataRole.UserRole + 2  # Envelope of rows that hold money
GROUP_ROLE = Qt.ItemDataRole.UserRole + 3  # group key (group rows)
STATE_ROLE = Qt.ItemDataRole.UserRole + 4  # colour key of the Available column
CHIP_ROLE = Qt.ItemDataRole.UserRole + 5  # chip colour behind a group name (Rosé Pine)
# (share 0..1, behind this month) of a target: a progress bar under the target text
PROGRESS_ROLE = Qt.ItemDataRole.UserRole + 6
PROGRESS_WIDTH = 140  # px at most: a bar, not a line across the wide column

ALL, OVERSPENT, UNDERFUNDED = "all", "overspent", "underfunded"


STATES = ["neg", "warn", "pos", "zero"]  # from the most to the least urgent


def _state(c: CategoryMonth) -> str:
    """Colour key of a budget line's available amount."""
    if c.overspent:
        return "neg"
    if c.underfunded:
        return "warn"
    return "pos" if c.available > 0 else "zero"


def _colors() -> dict[str, QColor]:
    t = theme.tokens()
    if t is not None:
        return {
            "neg": QColor(t["neg"]),
            "warn": QColor(t["warn"]),
            "pos": QColor(t["pos"]),
            "zero": QColor(t["line"]),
            "group": QColor(t["group_row"]),
        }
    if is_dark():
        return {
            "neg": QColor("#7a2828"),
            "warn": QColor("#6b5a10"),
            "pos": QColor("#1f5a33"),
            "zero": QColor("#3a3a3a"),
            "group": QColor("#2b2b2b"),
        }
    return {
        "neg": QColor("#f8d0cc"),
        "warn": QColor("#fbe9a7"),
        "pos": QColor("#cdeccf"),
        "zero": QColor("#ececec"),
        "group": QColor("#f3f3f3"),
    }


class AssignedDelegate(QStyledItemDelegate):
    """Inline editor for the Assigned column; accepts "600", "+50", "-20", "=0"."""

    def __init__(self, view: BudgetView) -> None:
        super().__init__(view)
        self.view = view

    def paint(self, painter, option, index):
        """Rosé Pine: the Available amount as a rounded pill, group names on a chip;
        a thin progress bar under each target."""
        if index.column() == COL_TARGET and index.data(PROGRESS_ROLE) is not None:
            self._paint_progress(painter, option, index)
            return
        t = theme.tokens()
        state = index.data(STATE_ROLE)
        chip = index.data(CHIP_ROLE)
        if t is None or (index.column() != COL_AVAILABLE or not state) and not chip:
            super().paint(painter, option, index)
            return
        selected = option.state & QStyle.StateFlag.State_Selected
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        # the row's colour (group rows) around the pill, not the state colour of the cell
        row = index.siblingAtColumn(COL_NAME).data(Qt.ItemDataRole.BackgroundRole)
        if selected:
            painter.fillRect(option.rect, QColor(t["accent_soft"]))
        elif row is not None:
            painter.fillRect(option.rect, row)
        font = index.data(Qt.ItemDataRole.FontRole) or option.font
        painter.setFont(font)
        text = str(index.data() or "")
        width = option.fontMetrics.horizontalAdvance(text) + 20
        r = option.rect.adjusted(4, 3, -4, -3)
        if chip:  # a pastel chip around the group name, left
            r.setLeft(option.rect.left() + 2)
            r.setWidth(min(width + 4, option.rect.width() - 6))
            fill, ink = QColor(chip), QColor(t["text"])
            align = Qt.AlignmentFlag.AlignCenter
        elif state == "zero":  # nothing to point out: muted text, no pill
            r.setLeft(max(r.left(), r.right() - width))
            fill, ink = QColor(0, 0, 0, 0), QColor(t["muted"])
            align = Qt.AlignmentFlag.AlignCenter
        else:  # a pill around the amount, right-aligned
            r.setLeft(max(r.left(), r.right() - width))
            fill = QColor(t[state]) if state != "zero" else QColor(t["line"])
            ink = QColor(t[state + "_text"]) if state != "zero" else QColor(t["muted"])
            align = Qt.AlignmentFlag.AlignCenter
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(fill)
        painter.drawRoundedRect(r, r.height() / 2, r.height() / 2)
        painter.setPen(ink)
        painter.drawText(r, align, text)
        painter.restore()

    def _paint_progress(self, painter, option, index) -> None:
        """The target text a little higher, a progress bar under it: the share funded
        (green), or behind this month (amber)."""
        share, behind = index.data(PROGRESS_ROLE)
        text_option = QStyleOptionViewItem(option)
        text_option.rect = option.rect.adjusted(0, -3, 0, -3)
        painter.save()
        painter.fillRect(
            option.rect, index.data(Qt.ItemDataRole.BackgroundRole) or QColor(0, 0, 0, 0)
        )
        painter.restore()
        super().paint(painter, text_option, index)
        r = option.rect
        track = QRectF(r.left() + 6, r.bottom() - 5, min(PROGRESS_WIDTH, r.width() - 12), 4)
        if track.width() <= 0:
            return
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        t = theme.tokens()
        painter.setBrush(QColor(t["line"]) if t is not None else QColor(128, 128, 128, 60))
        painter.drawRoundedRect(track, 2, 2)
        if share > 0:
            done = QRectF(track)
            done.setWidth(max(4.0, track.width() * min(1.0, share)))
            painter.setBrush(QColor(theme.color("warn_text" if behind else "pos_text")))
            painter.drawRoundedRect(done, 2, 2)
        painter.restore()

    def sizeHint(self, option, index):
        size = super().sizeHint(option, index)
        if theme.tokens() is not None:  # room around the pills
            size.setHeight(size.height() + 8)
        else:  # room for the target progress bars (all rows: uniform row heights)
            size.setHeight(size.height() + 6)
        return size

    def createEditor(self, parent, option, index):
        if index.column() != COL_ASSIGNED or index.data(ENV_ROLE) is None:
            return None
        edit = QLineEdit(parent)
        edit.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        edit.setPlaceholderText("600 / +50 / -20")
        return edit

    def setEditorData(self, editor: QLineEdit, index):
        c = self.view.envelope(index.data(ENV_ROLE))
        editor.setText(money(c.assigned, currency=None) if c and c.assigned else "")
        editor.selectAll()

    def setModelData(self, editor: QLineEdit, model, index):
        text = editor.text().strip()
        env = index.data(ENV_ROLE)
        c = self.view.envelope(env)
        if c is None:
            return
        try:
            cents = apply_amount_input(text, c.assigned) if text else 0
        except ValueError:
            show_error(
                self.view,
                self.view.tr("Assign"),
                self.view.tr("'%1' is not a valid amount.").replace("%1", text),
            )
            return
        # Refreshing rebuilds the tree; do it after Qt has finished committing the edit.
        QTimer.singleShot(0, lambda: self.view.set_assigned(env, cents))


@dataclass
class Step:
    """One undoable change of a month's assignments."""

    month: str
    label: str
    before: dict[Envelope, int]
    after: dict[Envelope, int]


class BudgetView(QWidget):
    #: (category_id, month) - show the bookings behind an activity
    show_transactions = Signal(int, str)
    #: month - show uncategorized bookings of that month
    show_uncategorized = Signal(str)
    #: money was assigned, moved or an assignment undone (the overview shows it)
    assignments_changed = Signal()

    def __init__(self, app: App, parent=None) -> None:
        super().__init__(parent)
        self.app = app
        self.month = month_of(date.today())
        self.data: BudgetMonth | None = None
        self._group_colors: dict[int, str] = {}  # chip colours
        self.undo_steps: list[Step] = []
        self.redo_steps: list[Step] = []

        header = self._build_header()
        toolbar, actions = self._build_actions()
        table = self._build_tree()

        layout = QVBoxLayout(self)
        layout.addLayout(header)
        layout.addSpacing(4)
        layout.addWidget(self.warning)
        layout.addLayout(toolbar)
        layout.addLayout(table, 1)
        layout.addLayout(actions)
        # Return / F2 on a row: type the amount to assign
        shortcuts.bind(  # not while the amount is being typed
            "budget_assign", self.tree, self._edit_assigned, Qt.ShortcutContext.WidgetShortcut
        )
        self.setFocusProxy(self.tree)
        self._update_undo_buttons()
        self.refresh()

    def _build_header(self) -> QHBoxLayout:
        """Month title with its arrows, the Ready to Assign terms and card, the warning."""
        # -- header --
        self.prev_button = QPushButton()
        self.next_button = QPushButton()
        set_glyph(self.prev_button, ICON_PREVIOUS, "◀")
        set_glyph(self.next_button, ICON_NEXT, "▶")
        for b in (self.prev_button, self.next_button):
            b.setFixedWidth(36)
        self.prev_button.clicked.connect(lambda: self.go(month_add(self.month, -1)))
        self.next_button.clicked.connect(lambda: self.go(month_add(self.month, 1)))
        today = QPushButton(self.tr("Today"))
        today.clicked.connect(lambda: self.go(month_of(date.today())))
        self.month_label = page_title("")

        # Ready to Assign as a card: caption, amount, how it is calculated
        self.rta_text = caption("")
        self.rta_amount = QLabel()
        rta_font = self.rta_amount.font()
        rta_font.setPointSize(rta_font.pointSize() + 12)
        rta_font.setBold(True)
        self.rta_amount.setFont(rta_font)
        self.rta_formula = QLabel()
        self.rta_box = QFrame()
        self.rta_box.setObjectName("rtaCard")
        self.rta_box.setMinimumWidth(280)
        rta_layout = QVBoxLayout(self.rta_box)
        rta_layout.setContentsMargins(22, 14, 22, 14)
        rta_layout.setSpacing(2)
        rta_layout.addWidget(self.rta_text)
        rta_layout.addWidget(self.rta_amount)
        rta_layout.addWidget(self.rta_formula)

        self.undo_button = icon_button(ICON_UNDO, "↶", self.tr("Undo"), self.undo)
        self.redo_button = icon_button(ICON_REDO, "↷", self.tr("Redo"), self.redo)
        self._bind_keys()
        self.prev_button.setToolTip(shortcuts.tip(self.tr("Previous month"), "budget_month", 0))
        self.next_button.setToolTip(shortcuts.tip(self.tr("Next month"), "budget_month", 1))
        today.setToolTip(shortcuts.tip(self.tr("Current month"), "budget_today"))

        # the month at the top, browsed with the arrows right next to it
        title_row = QHBoxLayout()
        title_row.addWidget(self.month_label)
        title_row.addSpacing(8)
        for w in (self.prev_button, self.next_button, today):
            title_row.addWidget(w, 0, Qt.AlignmentFlag.AlignVCenter)
        title_row.addStretch(1)

        # -- how Ready to Assign comes about: one row per term, with a tooltip --
        names = ("balance", "inflow", "carried", "assigned", "future", "result")
        self.term_names = {name: QLabel() for name in names}
        self.terms = {name: QLabel() for name in names}  # the amounts
        terms_grid = QGridLayout()
        terms_grid.setHorizontalSpacing(24)
        terms_grid.setVerticalSpacing(4)
        for row, name in enumerate(names):
            label, amount = self.term_names[name], self.terms[name]
            label.setObjectName("propName")
            label.setCursor(Qt.CursorShape.WhatsThisCursor)  # hints at the tooltip
            amount.setTextFormat(Qt.TextFormat.RichText)
            amount.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            terms_grid.addWidget(label, row, 0)
            terms_grid.addWidget(amount, row, 1)
        terms_grid.setColumnStretch(2, 1)

        left = QVBoxLayout()
        left.addLayout(title_row)
        left.addSpacing(6)
        left.addLayout(terms_grid)
        header = QHBoxLayout()
        header.addLayout(left, 1)
        header.addWidget(self.rta_box, 0, Qt.AlignmentFlag.AlignTop)

        self.warning = banner(QLabel(), "warning")
        self.warning.setTextFormat(Qt.TextFormat.RichText)
        self.warning.linkActivated.connect(lambda _l: self.show_uncategorized.emit(self.month))
        return header

    def _bind_keys(self) -> None:
        """The page's shortcuts (only while it is shown)."""
        context = Qt.ShortcutContext.WidgetWithChildrenShortcut  # only on this tab
        shortcuts.bind("undo_redo", self, self.undo, context, index=0)
        shortcuts.bind("undo_redo", self, self.redo, context, index=1)
        for index, step in ((0, -1), (1, 1)):  # Alt+Left / Right stay back / forward (#66)
            shortcuts.bind(
                "budget_month",
                self,
                lambda s=step: self.go(month_add(self.month, s)),
                context,
                index=index,
            )
        shortcuts.bind("budget_today", self, lambda: self.go(month_of(date.today())), context)
        shortcuts.bind("propose_budget", self, self.propose_budget, context)
        for index, expand in ((0, True), (1, False)):
            shortcuts.bind(
                "expand_groups",
                self,
                lambda e=expand: self._set_all_expanded(e),
                context,
                index=index,
            )
        shortcuts.bind("move_money", self, self._move_money_key, context)

    def _build_actions(self) -> tuple[QHBoxLayout, QHBoxLayout]:
        """The action bar below the table and the view options above it."""
        # -- actions (below the table): the frequent ones as buttons, the rest in a menu --
        more = QMenu(self)
        more.setToolTipsVisible(True)
        assign_targets = more.addAction(self.tr("Assign to targets"))
        assign_targets.setToolTip(
            self.tr("Fund what the targets need this month, as far as Ready to Assign allows.")
        )
        assign_targets.triggered.connect(self.assign_to_targets)
        self.cover_button = more.addAction(self.tr("Cover overspending"))
        self.cover_button.setToolTip(
            self.tr(
                "Assign to every overspent category what brings it back to 0, "
                "as far as Ready to Assign allows."
            )
        )
        self.cover_button.triggered.connect(self.cover_overspending)
        copy_last = more.addAction(self.tr("Copy last month"))
        copy_last.setToolTip(
            self.tr("Assign last month's amounts to categories with nothing assigned yet.")
        )
        copy_last.triggered.connect(self.copy_last_month)
        more.addSeparator()
        propose = QPushButton(self.tr("Propose budget…"))
        propose.setToolTip(
            shortcuts.tip(
                self.tr("Propose amounts from the spending of previous months."), "propose_budget"
            )
        )
        propose.clicked.connect(self.propose_budget)
        move = QPushButton(self.tr("Move money…"))
        move.clicked.connect(lambda: self.move_money())
        self.clear_button = more.addAction(self.tr("Clear assigned"))
        self.clear_button.setToolTip(
            self.tr("Remove everything assigned in this month, to start anew (can be undone).")
        )
        self.clear_button.triggered.connect(self.clear_assigned)
        self.more_button = QPushButton(self.tr("More"))
        self.more_button.setMenu(more)
        self.filter = QComboBox()
        self.filter.addItem(self.tr("All categories"), ALL)
        self.filter.addItem(self.tr("Overspent"), OVERSPENT)
        self.filter.addItem(self.tr("Underfunded"), UNDERFUNDED)
        ui = self.app.ui_state
        self.filter.setCurrentIndex(max(0, self.filter.findData(ui.get("budget.filter", ALL))))
        self.filter.currentIndexChanged.connect(self._filter_changed)
        self.hide_empty = QCheckBox(self.tr("Hide empty"))
        self.hide_empty.setChecked(ui.get("budget.hide_empty", True))
        self.hide_empty.setToolTip(
            self.tr(
                "Hide categories with nothing from last month, assigned, spent or available "
                "and no target to fund. Uncheck to assign money to them."
            )
        )
        self.hide_empty.toggled.connect(self._hide_empty_changed)
        self.collapsed_groups: set[str] = set(ui.get("budget.collapsed", []))
        self.expand_button = QPushButton()
        self.expand_button.clicked.connect(self._toggle_expanded)
        actions = action_bar(
            [propose, move, self.more_button], [self.undo_button, self.redo_button]
        )
        # view options above the table, at its right
        toolbar = QHBoxLayout()
        toolbar.addStretch(1)
        toolbar.addWidget(self.expand_button)
        toolbar.addWidget(self.hide_empty)
        self.filter.setToolTip(self.tr("Show"))  # the entries say what is shown
        toolbar.addWidget(self.filter)
        return toolbar, actions

    def _build_tree(self) -> QVBoxLayout:
        """The category tree and the totals row below it."""
        # -- tree --
        self.tree = QTreeWidget()
        self.tree.setColumnCount(COLUMNS)
        self.tree.setHeaderLabels(
            [
                self.tr("Category"),
                self.tr("From last month"),
                self.tr("Assigned"),
                self.tr("Activity"),
                self.tr("Available"),
                self.tr("Target"),
            ]
        )
        header_item = self.tree.headerItem()
        for col, tip in (
            (
                COL_CARRY,
                self.tr(
                    "Available at the end of last month. Overspending is not carried: "
                    "it was taken from Ready to Assign."
                ),
            ),
            (COL_ASSIGNED, self.tr("Money given to the category this month.")),
            (COL_ACTIVITY, self.tr("Spending (negative) and refunds (positive) this month.")),
            (
                COL_AVAILABLE,
                self.tr("From last month + Assigned + Activity: what is left to spend."),
            ),
        ):
            header_item.setToolTip(col, tip)
        # Every border can be dragged (it resizes the column on its left); the target
        # column, the last one, takes the remaining width.
        h = self.tree.header()
        h.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        h.setStretchLastSection(True)
        h.setMinimumSectionSize(60)
        self.tree.setColumnWidth(COL_NAME, 280)
        for col in MONEY_COLUMNS:
            self.tree.setColumnWidth(col, 130)
        saved = self.app.ui_state.get("budget.header", "")
        if saved:
            h.restoreState(QByteArray.fromBase64(saved.encode()))
            h.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
            h.setStretchLastSection(True)
        self.tree.setItemDelegate(AssignedDelegate(self))
        # F2 / Enter are shortcuts that always edit Assigned (see _edit_assigned)
        self.tree.setEditTriggers(
            QTreeWidget.EditTrigger.DoubleClicked | QTreeWidget.EditTrigger.SelectedClicked
        )
        self.tree.setAlternatingRowColors(True)
        self.tree.setUniformRowHeights(True)
        self.tree.itemDoubleClicked.connect(self._double_clicked)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._context_menu)
        self.tree.itemExpanded.connect(lambda i: self._group_toggled(i, True))
        self.tree.itemCollapsed.connect(lambda i: self._group_toggled(i, False))

        # -- totals row, kept below the tree and aligned with its columns --
        bold = QFont()
        bold.setBold(True)
        self.totals = QTreeWidget()
        self.totals.setColumnCount(COLUMNS)
        self.totals.setHeaderHidden(True)
        self.totals.setRootIsDecorated(False)
        self.totals.setSelectionMode(QTreeWidget.SelectionMode.NoSelection)
        self.totals.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.totals.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.totals.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.totals.header().setStretchLastSection(False)
        self.totals_item = QTreeWidgetItem([self.tr("Total")] + [""] * (COLUMNS - 1))
        for col in range(COLUMNS):
            self.totals_item.setFont(col, bold)
        self._align(self.totals_item)
        self.totals.addTopLevelItem(self.totals_item)
        self.totals.setFixedHeight(self.totals.sizeHintForRow(0) + 2 * self.totals.frameWidth() + 2)
        h.sectionResized.connect(lambda *_: self._sync_totals())
        h.geometriesChanged.connect(self._sync_totals)  # e.g. the window was resized

        table = QVBoxLayout()
        table.setSpacing(0)
        table.addWidget(self.tree, 1)
        table.addWidget(self.totals)
        return table

    # -- data ------------------------------------------------------------------

    def nav_state(self) -> str:
        """The month shown, for going back here (history)."""
        return self.month

    def restore_nav_state(self, month: str) -> None:
        if month != self.month:
            self.go(month)

    def focus_regions(self) -> list[QWidget]:
        """F6: the view options above the table, the table, the buttons below it."""
        return [self.expand_button, self.tree, self.undo_button]

    def go(self, month: str) -> None:
        self.month = month
        self.refresh()

    def refresh(self) -> None:
        with self.app.session() as s:
            self.data = budget.compute_month(s, self.app.settings, self.month)
            self._group_colors = group_colors(s)
        self.month = self.data.month  # clamped to the start month
        self._fill()

    def category(self, category_id: int) -> CategoryMonth | None:
        if self.data is None:
            return None
        return next((c for c in self.data.categories if c.category_id == category_id), None)

    def envelope(self, target: int | Envelope | None) -> CategoryMonth | None:
        """Budget line holding money: a category id / Envelope of a category or group."""
        if self.data is None or target is None:
            return None
        return self.data.envelope(target)

    # -- display ---------------------------------------------------------------

    def _month_title(self, month: str) -> str:
        y, m = map(int, month.split("-"))
        return f"{QLocale().standaloneMonthName(m)} {y}"

    def _fill(self) -> None:
        b = self.data
        if b is None:
            return
        colors = _colors()
        self.month_label.setText(self._month_title(b.month))
        self.prev_button.setEnabled(b.month > b.start_month)

        rta = b.ready_to_assign
        if rta >= 0:
            self.rta_text.setText(self.tr("Ready to Assign").upper())
            box = colors["pos"] if rta > 0 else colors["zero"]
        else:
            self.rta_text.setText(self.tr("Assigned too much - remove from categories").upper())
            box = colors["neg"]
        self.rta_amount.setText(money(rta))
        t = theme.tokens()
        if t is not None:  # a card with the design's ink
            ink = t["neg_text"] if rta < 0 else t["pos_text"] if rta > 0 else t["muted"]
            radius = 14
        else:
            ink = amount_color(-1).name() if rta < 0 else "palette(text)"
            radius = 8
        self.rta_box.setStyleSheet(
            f"QFrame#rtaCard {{ background: {box.name()}; border-radius: {radius}px; }}"
            f"QFrame#rtaCard QLabel {{ background: transparent; color: {ink}; }}"
        )

        if b.uncategorized_count:
            attention = self.tr("Attention")
            self.warning.setText(
                f"⚠ <b>{attention}</b>&nbsp; "
                + self.tr(
                    "%n uncategorized booking(s) (%1) affect Ready to Assign. "
                    "<a href='#'>Categorize them</a>",
                    "",
                    b.uncategorized_count,
                )
                .replace("%1", money(b.uncategorized_amount))
                # readable in light and dark mode (the default link blue is not)
                .replace("<a ", f"<a style='color: {accent_color().name()}' ")
                .replace("</a>", " →</a>")
            )
            self.warning.show()
        else:
            self.warning.hide()

        self._fill_totals(b)
        self._fill_breakdown(b)
        self.cover_button.setEnabled(any(e.overspent for e in b.envelopes))

        mode = self.filter.currentData()
        self.tree.clear()
        bold = QFont()
        bold.setBold(True)
        for g in b.groups:
            gi = self._group_item(g, mode, colors, bold)
            if gi is None:
                continue
            self.tree.addTopLevelItem(gi)
            gi.setExpanded(g.key not in self.collapsed_groups)
        self._update_expand_button()

    def _filter_changed(self) -> None:
        self.app.ui_state.set("budget.filter", self.filter.currentData())
        self._fill()

    def _hide_empty_changed(self, checked: bool) -> None:
        self.app.ui_state.set("budget.hide_empty", checked)
        self._fill()

    def _group_toggled(self, item: QTreeWidgetItem, expanded: bool) -> None:
        key = item.data(COL_NAME, GROUP_ROLE)
        if key is not None:
            (self.collapsed_groups.discard if expanded else self.collapsed_groups.add)(key)
            self.app.ui_state.set("budget.collapsed", sorted(self.collapsed_groups))
        self._update_expand_button()

    def _groups(self) -> list[QTreeWidgetItem]:
        return [self.tree.topLevelItem(i) for i in range(self.tree.topLevelItemCount())]

    def _toggle_expanded(self) -> None:
        self._set_all_expanded(not any(g.isExpanded() for g in self._groups()))

    def _set_all_expanded(self, expand: bool) -> None:
        for g in self._groups():
            g.setExpanded(expand)  # remembered via itemExpanded / itemCollapsed
        self._update_expand_button()

    def _update_expand_button(self) -> None:
        if any(g.isExpanded() for g in self._groups()):
            self.expand_button.setText(self.tr("Collapse all"))
            self.expand_button.setToolTip(shortcuts.native(shortcuts.sequences("expand_groups")[1]))
        else:
            self.expand_button.setText(self.tr("Expand all"))
            self.expand_button.setToolTip(shortcuts.native(shortcuts.sequences("expand_groups")[0]))

    def save_state(self) -> None:
        header = self.tree.header().saveState()
        self.app.ui_state.set("budget.header", header.toBase64().data().decode())

    def _sync_totals(self) -> None:
        h = self.tree.header()
        for col in range(COLUMNS):
            self.totals.setColumnWidth(col, h.sectionSize(col))

    def _fill_totals(self, b: BudgetMonth) -> None:
        t = self.totals_item
        t.setText(COL_CARRY, money(b.carry))
        t.setToolTip(
            COL_CARRY,
            self.tr(
                "Money left in the categories at the end of last month. Overspending is not "
                "carried: it was taken from Ready to Assign."
            ),
        )
        t.setText(COL_ASSIGNED, money(b.assigned))
        t.setText(COL_ACTIVITY, money(b.activity))
        t.setText(COL_AVAILABLE, money(b.available))
        t.setToolTip(COL_NAME, self.tr("Sum of all categories, also when a filter is set."))
        t.setToolTip(
            COL_ASSIGNED,
            self.tr("Money given to the categories this month. It comes from Ready to Assign."),
        )
        t.setToolTip(
            COL_ACTIVITY,
            self.tr(
                "Spending (negative) and refunds (positive) in the categories this month.\n"
                "Income is not included: it goes to Ready to Assign."
            ),
        )
        lines = [
            self.tr("Money left in the categories: from last month + assigned + activity."),
            "",
            self.tr("Available: %1").replace("%1", money(b.available)),
            self.tr("+ Ready to Assign: %1").replace("%1", money(b.ready_to_assign)),
        ]
        if b.assigned_in_future:
            lines.append(
                self.tr("+ Assigned in later months: %1").replace("%1", money(b.assigned_in_future))
            )
        lines.append(
            self.tr("= %1 on the budget accounts at the end of the month").replace(
                "%1", money(b.balance)
            )
        )
        t.setToolTip(COL_AVAILABLE, "\n".join(lines))
        self._sync_totals()

    def _fill_breakdown(self, b: BudgetMonth) -> None:
        """Bottom line: how Ready to Assign is made up, one term per label with a tooltip."""
        first_month = b.month == b.start_month
        terms = {
            # name: (contribution, text, tooltip, always shown)
            "balance": (
                b.balance_last_month,
                self.tr("Start funds") if first_month else self.tr("Balance end of last month"),
                self.tr("Balance of the budget accounts when the budget starts.")
                if first_month
                else self.tr(
                    "Balance of all budget accounts at the end of last month. Last month's "
                    "spending is already in it; this month's spending comes out of the "
                    "categories, not out of Ready to Assign."
                ),
                True,
            ),
            "inflow": (
                b.inflow,
                self.tr("Inflow"),
                self.tr(
                    "Money that came in this month: income categories, uncategorized bookings "
                    "and transfers from tracking accounts."
                ),
                True,
            ),
            "carried": (
                -b.carried_in,
                self.tr("Kept in categories"),
                self.tr(
                    'Money the categories kept from last month (column "From last month"). '
                    "It is in the balance, but already given to categories."
                ),
                False,
            ),
            "assigned": (
                -b.assigned_this_month,
                self.tr("Assigned"),
                self.tr("Money given to the categories this month."),
                True,
            ),
            "future": (
                -b.assigned_in_future,
                self.tr("Assigned in later months"),
                self.tr("Money already given to categories in the months after this one."),
                False,
            ),
        }
        tooltip_lines = []
        formula = []  # on the card: "14.343,32 + 458,00 − 14.457,61"
        for i, (name, (cents, text, tip, always)) in enumerate(terms.items()):
            label, amount_label = self.term_names[name], self.terms[name]
            shown = always or cents != 0
            label.setVisible(shown)
            amount_label.setVisible(shown)
            if not shown:
                continue
            op = "" if i == 0 else ("− " if cents < 0 else "+ ")
            amount = money(cents if i == 0 else abs(cents))
            label.setText(text)
            amount_label.setText(f"<b>{op}{amount}</b>")
            for w in (label, amount_label):
                w.setToolTip(tip)
            tooltip_lines.append(f"{op}{text}: {amount}")
            formula.append(op + money(cents if i == 0 else abs(cents), currency=None))
        result_tip = self.tr(
            "Money not given to any category yet: assign it until this is 0.\n"
            "Negative: more was assigned than there is - take money back from categories."
        )
        self.term_names["result"].setText(f"= {self.tr('Ready to Assign')}")
        self.terms["result"].setText(f"<b>{money(b.ready_to_assign)}</b>")
        for w in (self.term_names["result"], self.terms["result"]):
            w.setToolTip(result_tip)
        self.rta_formula.setText(" ".join(formula))
        tooltip_lines.append(f"= {self.tr('Ready to Assign')}: {money(b.ready_to_assign)}")
        self.rta_box.setToolTip("\n".join(tooltip_lines))

    def _matches(self, c: CategoryMonth, mode: str) -> bool:
        if self.hide_empty.isChecked() and not (
            c.carry or c.assigned or c.activity or c.available or c.needed
        ):
            return False
        return (
            mode == ALL
            or (mode == OVERSPENT and c.overspent)
            or (mode == UNDERFUNDED and c.underfunded)
        )

    def _group_item(
        self, g: GroupMonth, mode: str, colors: dict[str, QColor], bold: QFont
    ) -> QTreeWidgetItem | None:
        if g.budget is not None:
            if not self._matches(g.budget, mode):
                return None
            # its categories only show their activity
            cats = [c for c in g.categories if c.activity or not self.hide_empty.isChecked()]
        else:
            cats = [c for c in g.categories if self._matches(c, mode)]
            if not cats:
                return None
        gi = QTreeWidgetItem([g.name] + [""] * (COLUMNS - 1))
        gi.setText(COL_CARRY, money(g.carry))
        gi.setText(COL_ASSIGNED, money(g.assigned))
        gi.setText(COL_ACTIVITY, money(g.activity))
        gi.setText(COL_AVAILABLE, money(g.available))
        gi.setData(COL_NAME, GROUP_ROLE, g.key)
        if g.group_id in self._group_colors:
            gi.setData(COL_NAME, CHIP_ROLE, self._group_colors[g.group_id])
        for col in range(COLUMNS):
            gi.setFont(col, bold)
            gi.setBackground(col, QBrush(colors["group"]))
        self._align(gi)
        if g.budget is not None:
            self._money_line(gi, g.budget, colors, bold)
            gi.setToolTip(
                COL_NAME,
                self.tr(
                    "Budgeted as a whole: the categories share one amount. "
                    "Right-click to budget per category."
                ),
            )
        else:
            gi.setToolTip(COL_NAME, self.tr("Right-click to budget the group as a whole."))
            self._group_state(gi, g, colors)
        for c in cats:
            item = QTreeWidgetItem([c.name] + [""] * (COLUMNS - 1))
            item.setText(COL_ACTIVITY, money(c.activity))
            item.setData(COL_NAME, CAT_ROLE, c.category_id)
            self._align(item)
            if c.in_group_budget:
                item.setForeground(COL_NAME, QBrush(QColor("#888888")))
                item.setToolTip(COL_NAME, self.tr("Spends from the group's budget"))
            else:
                self._money_line(item, c, colors, bold)
            item.setToolTip(COL_ACTIVITY, self.tr("Double-click to show the bookings"))
            gi.addChild(item)
        return gi

    def _group_state(self, gi: QTreeWidgetItem, g: GroupMonth, colors: dict[str, QColor]) -> None:
        """Colour of a group budgeted per category: the worst state of its categories
        (also of those hidden by a filter), so a problem shows when it is collapsed."""
        states = [(_state(c), c) for c in g.categories]
        worst = min((STATES.index(state) for state, _ in states), default=STATES.index("zero"))
        gi.setBackground(COL_AVAILABLE, QBrush(colors[STATES[worst]]))
        gi.setData(COL_AVAILABLE, STATE_ROLE, STATES[worst])
        overspent = [c.name for state, c in states if state == "neg"]
        underfunded = [c.name for state, c in states if state == "warn"]
        lines = []
        if overspent:
            lines.append(self.tr("Overspent: %1").replace("%1", ", ".join(overspent)))
        if underfunded:
            lines.append(self.tr("Target not funded: %1").replace("%1", ", ".join(underfunded)))
        gi.setToolTip(COL_AVAILABLE, "\n".join(lines) or self.tr("All categories are fine."))

    def _money_line(
        self, item: QTreeWidgetItem, c: CategoryMonth, colors: dict[str, QColor], bold: QFont
    ) -> None:
        """Fill and color the columns of a line that holds money (category or group)."""
        item.setText(COL_CARRY, money(c.carry))
        item.setText(COL_ASSIGNED, money(c.assigned))
        item.setText(COL_AVAILABLE, money(c.available))
        item.setText(COL_TARGET, self._target_text(c))
        item.setToolTip(COL_TARGET, self._target_text(c))  # the column may cut it off
        if c.target is not None and c.progress is not None:
            item.setData(COL_TARGET, PROGRESS_ROLE, (c.progress, c.needed > 0))
        item.setData(COL_NAME, ENV_ROLE, c.envelope)
        item.setData(COL_ASSIGNED, ENV_ROLE, c.envelope)
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
        item.setBackground(COL_AVAILABLE, QBrush(colors[_state(c)]))
        item.setData(COL_AVAILABLE, STATE_ROLE, _state(c))
        item.setFont(COL_AVAILABLE, bold)
        item.setToolTip(COL_AVAILABLE, self._available_tooltip(c))
        item.setToolTip(COL_ASSIGNED, self.tr("Double-click to assign: 600, +50, -20"))

    def _align(self, item: QTreeWidgetItem) -> None:
        for col in MONEY_COLUMNS:
            item.setTextAlignment(col, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

    def _available_tooltip(self, c: CategoryMonth) -> str:
        lines = [
            self.tr("From last month: %1").replace("%1", money(c.carry)),
            self.tr("Assigned: %1").replace("%1", money(c.assigned)),
            self.tr("Activity: %1").replace("%1", money(c.activity)),
        ]
        if c.overspent:
            lines.append(
                self.tr("Overspent: move money here or it is taken from next month's budget.")
            )
        return "\n".join(lines)

    def _target_text(self, c: CategoryMonth) -> str:
        t = c.target
        if t is None:
            return ""
        amount = money(round(t.amount * 100))
        if t.type == "monthly":
            text = self.tr("%1 monthly").replace("%1", amount)
        elif t.type == "balance" and c.due is None:
            text = self.tr("Balance of %1").replace("%1", amount)
        else:
            due = self._month_title(c.due) if c.due else ""
            if t.type == "repeating":
                text = self.tr("%1 every %2 months, next %3")
                text = text.replace("%2", str(t.every_months)).replace("%3", due)
            else:
                text = self.tr("%1 by %2").replace("%2", due)
            text = text.replace("%1", amount)
        if c.needed > 0:
            text += " – " + self.tr("%1 more needed").replace("%1", money(c.needed))
        elif c.progress is not None:
            text += " – " + (
                self.tr("funded") if c.progress >= 1 else f"{round(c.progress * 100)} %"
            )
        return text

    # -- actions ---------------------------------------------------------------

    def _run(self, title: str, fn) -> bool:
        """Change this month's assignments; the change can be undone as ``title``."""
        try:
            with self.app.session() as s:
                before = budget.assignments_of(s, self.month)
                fn(s)
                s.flush()
                after = budget.assignments_of(s, self.month)
        except Exception as e:
            show_error(self, title, e)
            return False
        self._record(Step(self.month, title, before, after))
        self.refresh()
        return True

    def _record(self, step: Step) -> None:
        if step.before != step.after:
            self.undo_steps.append(step)
            self.redo_steps.clear()
            self.assignments_changed.emit()
        self._update_undo_buttons()

    def _update_undo_buttons(self) -> None:
        for button, steps, name, text, index in (
            (self.undo_button, self.undo_steps, self.tr("Undo"), self.tr("Undo: %1 (%2)"), 0),
            (self.redo_button, self.redo_steps, self.tr("Redo"), self.tr("Redo: %1 (%2)"), 1),
        ):
            button.setEnabled(bool(steps))
            if steps:  # what would be undone / redone
                name = text.replace("%1", steps[-1].label).replace(
                    "%2", self._month_title(steps[-1].month)
                )
            button.setToolTip(shortcuts.tip(name, "undo_redo", index))

    def _edit_assigned(self) -> None:
        item = self.tree.currentItem()
        if item is not None and item.data(COL_NAME, ENV_ROLE) is not None:
            self.tree.setCurrentItem(item, COL_ASSIGNED)
            self.tree.editItem(item, COL_ASSIGNED)

    def _move_money_key(self) -> None:
        item = self.tree.currentItem()
        self.move_money(item.data(COL_NAME, ENV_ROLE) if item is not None else None)

    def _restore(self, step: Step, amounts: dict[Envelope, int]) -> bool:
        try:
            with self.app.session() as s:
                budget.restore_assignments(s, step.month, amounts)
        except Exception as e:
            show_error(self, self.tr("Undo"), e)
            return False
        self.assignments_changed.emit()
        self.go(step.month)  # show the month that changed
        return True

    def undo(self) -> None:
        if self.undo_steps and self._restore(self.undo_steps[-1], self.undo_steps[-1].before):
            self.redo_steps.append(self.undo_steps.pop())
        self._update_undo_buttons()

    def redo(self) -> None:
        if self.redo_steps and self._restore(self.redo_steps[-1], self.redo_steps[-1].after):
            self.undo_steps.append(self.redo_steps.pop())
        self._update_undo_buttons()

    def clear_assigned(self) -> None:
        if (
            QMessageBox.question(
                self,
                self.tr("Clear assigned"),
                self.tr(
                    "Remove everything assigned in %1? Ready to Assign gets the money back. "
                    "You can undo this."
                ).replace("%1", self._month_title(self.month)),
            )
            == QMessageBox.StandardButton.Yes
        ):
            self._run(self.tr("Clear assigned"), lambda s: budget.clear_month(s, self.month))

    def set_assigned(self, target: int | Envelope, cents: int) -> None:
        self._run(
            self.tr("Assign"),
            lambda s: budget.set_assigned(s, self.month, target, cents),
        )

    def assign_to_targets(self) -> None:
        result: list[budget.AssignResult] = []
        if self._run(
            self.tr("Assign to targets"),
            lambda s: result.append(budget.assign_to_targets(s, self.app.settings, self.month)),
        ):
            r = result[0]
            msg = self.tr("%1 assigned to %2 categories.").replace("%1", money(r.amount))
            msg = msg.replace("%2", str(r.categories))
            if r.still_needed:
                msg += "\n" + self.tr(
                    "%1 is still needed, but Ready to Assign is used up."
                ).replace("%1", money(r.still_needed))
            QMessageBox.information(self, self.tr("Assign to targets"), msg)

    def cover_overspending(self) -> None:
        result: list[budget.AssignResult] = []
        if self._run(
            self.tr("Cover overspending"),
            lambda s: result.append(budget.cover_overspending(s, self.app.settings, self.month)),
        ):
            r = result[0]
            msg = self.tr("%1 assigned to %2 overspent categories.").replace("%1", money(r.amount))
            msg = msg.replace("%2", str(r.categories))
            if r.still_needed:
                msg += "\n" + self.tr(
                    "%1 is still overspent, but Ready to Assign is used up. "
                    "Move money from other categories to cover it."
                ).replace("%1", money(r.still_needed))
            QMessageBox.information(self, self.tr("Cover overspending"), msg)

    def propose_budget(self) -> None:
        if self.data is None:
            return
        with self.app.session() as s:
            before = budget.assignments_of(s, self.month)
        dlg = ProposalDialog(self.app, self.month, self.data.ready_to_assign, self)
        if dlg.exec() and dlg.applied:
            with self.app.session() as s:
                after = budget.assignments_of(s, self.month)
            self._record(Step(self.month, self.tr("Propose budget"), before, after))
            self.refresh()

    def copy_last_month(self) -> None:
        count: list[int] = []
        if self._run(
            self.tr("Copy last month"),
            lambda s: count.append(budget.copy_last_month(s, self.app.settings, self.month)),
        ):
            QMessageBox.information(
                self,
                self.tr("Copy last month"),
                self.tr("Amounts copied for %1 categories.").replace("%1", str(count[0])),
            )

    def move_money(self, target: int | Envelope | None = None) -> None:
        if self.data is None:
            return
        c = self.envelope(target)
        if c is not None and c.overspent:
            kwargs = {"to_id": c.envelope, "cents": -c.available}  # cover overspending
        elif c is not None:
            kwargs = {"from_id": c.envelope, "cents": max(c.available, 0)}
        else:  # nothing selected: from Ready to Assign to the line that needs money most
            kwargs = self._move_money_default()
        dlg = MoveMoneyDialog(self.data, self, **kwargs)
        if dlg.exec():
            self._run(
                self.tr("Move money"),
                lambda s: budget.move_money(s, self.month, dlg.from_id, dlg.to_id, dlg.cents),
            )

    def _move_money_default(self) -> dict:
        """To the first overspent line (its overspending), else the first underfunded one
        (what its target still needs), else the first line with no amount."""
        lines = [e for e in self.data.envelopes if not e.hidden]
        for wanted, cents in (
            (lambda e: e.overspent, lambda e: -e.available),
            (lambda e: e.underfunded, lambda e: e.needed),
        ):
            line = next((e for e in lines if wanted(e)), None)
            if line is not None:
                return {"to_id": line.envelope, "cents": cents(line)}
        return {"to_id": lines[0].envelope} if lines else {}

    def edit_target(self, target: int | Envelope) -> None:
        c = self.envelope(target)
        if c is None:
            return
        dlg = TargetDialog(c.name, c.target, self)
        if not dlg.exec():
            return
        new = None if dlg.removed else dlg.result_target
        try:
            if c.is_group:
                config_edit.save_group_target(self.app.settings, c.group_key, new)
            else:
                config_edit.save_target(self.app.settings, c.key, new)
        except Exception as e:
            show_error(self, self.tr("Target"), e)
            return
        self.refresh()

    def set_group_budget(self, group_key: str, by_group: bool, confirm: bool = True) -> None:
        """Budget a group as a whole or per category again (written to categories.yaml)."""
        g = next((g for g in self.data.groups if g.key == group_key), None) if self.data else None
        name = g.name if g else group_key
        if by_group:
            text = self.tr(
                "Budget '%1' as a whole?\n\nThe categories then share one amount. Amounts "
                "assigned to the categories are added up into the group (in all months); "
                "category targets are no longer used. Categorization is not affected."
            )
        else:
            text = self.tr(
                "Budget '%1' per category again?\n\nThe group's assigned amounts (all "
                "months) go to its first category - distribute them afterwards. "
                "The group target is removed."
            )
        if confirm and (
            QMessageBox.question(self, self.tr("Budget level"), text.replace("%1", name))
            != QMessageBox.StandardButton.Yes
        ):
            return
        try:
            config_edit.save_group_budget(self.app.settings, group_key, by_group)
            self.app.sync_config()
        except Exception as e:
            show_error(self, self.tr("Budget level"), e)
            return
        # Assignments of all months were converted: earlier steps no longer apply.
        self.undo_steps.clear()
        self.redo_steps.clear()
        self._update_undo_buttons()
        self.refresh()

    def _double_clicked(self, item: QTreeWidgetItem, column: int) -> None:
        cat_id = item.data(COL_NAME, CAT_ROLE)
        env = item.data(COL_NAME, ENV_ROLE)
        if column == COL_ACTIVITY and cat_id is not None:
            self.show_transactions.emit(cat_id, self.month)
        elif env is None:
            return
        elif column == COL_TARGET:
            self.edit_target(env)
        elif column == COL_AVAILABLE:
            self.move_money(env)

    def _context_menu(self, pos) -> None:
        item = self.tree.itemAt(pos)
        if item is None:
            return
        cat_id = item.data(COL_NAME, CAT_ROLE)
        env = item.data(COL_NAME, ENV_ROLE)
        group_key = item.data(COL_NAME, GROUP_ROLE)
        menu = QMenu(self)
        if env is not None:
            menu.addAction(self.tr("Edit target…"), lambda: self.edit_target(env))
            menu.addAction(self.tr("Move money…"), lambda: self.move_money(env))
        if cat_id is not None:
            menu.addAction(
                self.tr("Show bookings"), lambda: self.show_transactions.emit(cat_id, self.month)
            )
        if group_key is not None:
            if menu.actions():
                menu.addSeparator()
            if env is None:
                menu.addAction(
                    self.tr("Budget this group as a whole"),
                    lambda: self.set_group_budget(group_key, True),
                )
            else:
                menu.addAction(
                    self.tr("Budget per category"),
                    lambda: self.set_group_budget(group_key, False),
                )
        if menu.actions():
            menu.exec(self.tree.viewport().mapToGlobal(pos))
