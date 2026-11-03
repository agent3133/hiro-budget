"""Categories & rules tab: edit categories.yaml and rules.yaml from the GUI."""

from __future__ import annotations

import html
import re
import unicodedata
from dataclasses import dataclass, fields

from PySide6.QtCore import QSignalBlocker, Qt, QTimer, Signal
from PySide6.QtGui import QFontMetrics, QPalette
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QSplitter,
    QStyledItemDelegate,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from hiro_budget.app import App
from hiro_budget.config import (
    MAX_REPORT_SHIFT_DAYS,
    CategoriesConfig,
    CategoryDef,
    GroupDef,
    RuleDef,
    RuleMatch,
)
from hiro_budget.gui import regex_help, shortcuts
from hiro_budget.gui.common import show_error
from hiro_budget.gui.navigation import ICON_DELETE, ICON_DOWN, ICON_UP
from hiro_budget.gui.widgets import (
    DetailCard,
    Drop,
    DropTree,
    action_bar,
    banner,
    icon_button,
    page_title,
    primary,
    revert_by_key,
)
from hiro_budget.services import config_edit
from hiro_budget.services.categorization.pipeline import recategorize_all
from hiro_budget.services.config_edit import KeyRenames
from hiro_budget.services.sync import check_key_renames
from hiro_budget.services.transactions import TransactionRow

# key in categories.yaml (categories: full "group.category" key); None: new item
SAVED_KEY_ROLE = Qt.ItemDataRole.UserRole + 1
KEY_EDITED_ROLE = Qt.ItemDataRole.UserRole + 2  # key typed by the user (not from the name)
_UMLAUTS = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"})


def slugify(text: str) -> str:
    text = text.lower().translate(_UMLAUTS)
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "_", text).strip("_") or "neu"


def unique(base: str, taken: set[str]) -> str:
    key, i = base, 2
    while key in taken:
        key, i = f"{base}_{i}", i + 1
    return key


# --- categories ----------------------------------------------------------------------


class CategoryTree(DropTree):
    """Drag and drop: groups between groups, categories into any group."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)


SHIFT_COLUMN = 4  # report_shift_days of a category ("" for 0)


class ShiftDelegate(QStyledItemDelegate):
    """A spin box 0..MAX_REPORT_SHIFT_DAYS for the reporting-month column of categories;
    groups have none."""

    def createEditor(self, parent, option, index):
        if not index.parent().isValid():  # a group
            return None
        box = QSpinBox(parent)
        box.setRange(0, MAX_REPORT_SHIFT_DAYS)
        box.setSpecialValueText("–")  # 0: the booking date counts
        return box

    def setEditorData(self, editor: QSpinBox, index) -> None:
        editor.setValue(int(index.data() or 0))

    def setModelData(self, editor: QSpinBox, model, index) -> None:
        editor.interpretText()
        model.setData(index, str(editor.value()) if editor.value() else "")


def icon_buttons(editor, up: str, down: str, remove: str) -> list:
    """Up, down and remove below a list of the editors, their keys in the tooltips."""
    return [
        icon_button(ICON_UP, "↑", shortcuts.tip(up, "item_move", 0), lambda: editor._move(-1)),
        icon_button(ICON_DOWN, "↓", shortcuts.tip(down, "item_move", 1), lambda: editor._move(1)),
        icon_button(ICON_DELETE, "✕", shortcuts.tip(remove, "item_remove"), editor._remove),
    ]


def bind_list_keys(editor, view) -> None:
    """Del, Alt+Up / Down on the list (#68), the revert key on the whole editor."""
    on_list = Qt.ShortcutContext.WidgetShortcut
    shortcuts.bind("item_remove", view, editor._remove, on_list)
    for index, delta in ((0, -1), (1, 1)):
        shortcuts.bind("item_move", view, lambda d=delta: editor._move(d), on_list, index=index)
    shortcuts.bind(
        "revert",
        editor,
        lambda: revert_by_key(editor, editor.revert_button),
        Qt.ShortcutContext.WidgetWithChildrenShortcut,
    )


class CategoryEditor(QWidget):
    saved = Signal()
    #: unsaved changes appeared (True) or were saved / discarded (False)
    dirty_changed = Signal(bool)

    def __init__(self, app: App, parent=None) -> None:
        super().__init__(parent)
        self.app = app
        self.tree = CategoryTree()
        self.tree.setHeaderLabels(
            [
                self.tr("Name"),
                self.tr("Key"),
                # two lines where a language needs them: the columns hold only a check
                # box or a number, the names need the room (#38)
                self.tr("Income", "column header, may have two lines"),
                self.tr("One-off", "column header, may have two lines"),
                self.tr("Report\nmonth", "column header, may have two lines"),
            ]
        )
        self.tree.headerItem().setToolTip(
            SHIFT_COLUMN,
            self.tr(
                "In the reports, bookings up to this day of a month count for the previous "
                "month, e.g. 5 for an instalment due at the end of the month but debited on "
                "the 1st. Empty: the booking date counts. Double-click to change."
            ),
        )
        self.tree.setItemDelegateForColumn(SHIFT_COLUMN, ShiftDelegate(self.tree))
        self.tree.headerItem().setToolTip(
            3,
            self.tr(
                "Irregular income or expenses (bonus, tax refund, repairs): the reports can "
                "leave them out"
            ),
        )
        header = self.tree.header()
        header.setStretchLastSection(False)
        # the names as wide as the longest (set in _fit_name_column), the keys get the
        # rest; check boxes and the number as wide as their header
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for column in (2, 3, SHIFT_COLUMN):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.itemChanged.connect(self._item_changed)
        self.tree.dropped.connect(self.move_item)

        add_group = QPushButton(self.tr("Add group"))
        add_group.setToolTip(shortcuts.tip(self.tr("Add group"), "group_new"))
        add_group.clicked.connect(self._add_group)
        add_category = QPushButton(self.tr("Add category"))
        add_category.setToolTip(shortcuts.tip(self.tr("Add category"), "item_new"))
        add_category.clicked.connect(self._add_category)
        list_bar = action_bar(
            [add_group, add_category],
            icon_buttons(self, self.tr("Up"), self.tr("Down"), self.tr("Remove")),
        )
        page = Qt.ShortcutContext.WidgetWithChildrenShortcut
        shortcuts.bind("item_new", self, self._add_category, page)
        shortcuts.bind("group_new", self, self._add_group, page)
        bind_list_keys(self, self.tree)
        self.save_button = QPushButton(self.tr("Save categories"))
        self.save_button.setDefault(True)  # main action: accent colour
        self.save_button.clicked.connect(self.save)
        self.revert_button = QPushButton(self.tr("Revert"))
        self.revert_button.clicked.connect(self.load)
        save_row = QHBoxLayout()
        save_row.setContentsMargins(0, 10, 0, 0)
        save_row.addStretch(1)
        save_row.addWidget(self.revert_button)
        save_row.addWidget(self.save_button)

        layout = QVBoxLayout(self)
        layout.addWidget(self.tree, 1)
        layout.addLayout(list_bar)
        layout.addLayout(save_row)
        self.load()

    def _fit_name_column(self) -> None:
        """The name column as wide as its longest name (measured with QFontMetrics: the
        tree's sizeHintForColumn is slow through the stylesheet)."""
        tree = self.tree
        indent = tree.indentation()
        widest = QFontMetrics(tree.header().font()).horizontalAdvance(tree.headerItem().text(0))
        for i in range(tree.topLevelItemCount()):
            group = tree.topLevelItem(i)
            items = [(group, 1)] + [(group.child(j), 2) for j in range(group.childCount())]
            for item, depth in items:
                text = QFontMetrics(item.font(0)).horizontalAdvance(item.text(0))
                widest = max(widest, depth * indent + text)
                if item.toolTip(1) != item.text(1):  # the key column may be cut
                    with QSignalBlocker(tree):
                        item.setToolTip(1, item.text(1))
        tree.setColumnWidth(0, widest + 24)  # cell padding of the theme

    def _set_dirty(self, dirty: bool) -> None:
        self._fit_name_column()
        self.save_button.setEnabled(dirty)
        self.revert_button.setEnabled(dirty)
        self.dirty_changed.emit(dirty)

    def load(self) -> None:
        self.tree.blockSignals(True)
        self.tree.clear()
        for g in self.app.settings.categories.groups:
            gi = self._group_item(g.key, g.name, g.income)
            for c in g.categories:
                self._category_item(
                    gi,
                    c.key,
                    c.name,
                    saved=f"{g.key}.{c.key}",
                    one_off=c.one_off,
                    shift_days=c.report_shift_days,
                )
        self.tree.expandAll()
        self.tree.blockSignals(False)
        self._set_dirty(False)

    def _item_changed(self, item: QTreeWidgetItem, column: int) -> None:
        self.tree.blockSignals(True)
        try:
            if column == 1:
                key = slugify(item.text(1))
                item.setText(1, unique(key, self._sibling_keys(item)))
                item.setData(0, KEY_EDITED_ROLE, True)
            elif column == 0 and item.data(0, SAVED_KEY_ROLE) is None:
                if not item.data(0, KEY_EDITED_ROLE):
                    # A new item's key follows its name until the key is edited.
                    item.setText(1, unique(slugify(item.text(0)), self._sibling_keys(item)))
        finally:
            self.tree.blockSignals(False)
        self._set_dirty(True)

    def _sibling_keys(self, item: QTreeWidgetItem) -> set[str]:
        parent = item.parent()
        siblings = (
            [parent.child(i) for i in range(parent.childCount())]
            if parent is not None
            else [self.tree.topLevelItem(i) for i in range(self.tree.topLevelItemCount())]
        )
        return {s.text(1) for s in siblings if s is not item}

    def _group_item(
        self, key: str, name: str, income: bool, index: int | None = None, saved: bool = True
    ):
        item = QTreeWidgetItem([name, key, ""])
        item.setData(0, SAVED_KEY_ROLE, key if saved else None)
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable | Qt.ItemFlag.ItemIsUserCheckable)
        item.setCheckState(2, Qt.CheckState.Checked if income else Qt.CheckState.Unchecked)
        font = item.font(0)
        font.setBold(True)
        item.setFont(0, font)
        if index is None:
            self.tree.addTopLevelItem(item)
        else:
            self.tree.insertTopLevelItem(index, item)
        return item

    def _category_item(
        self,
        parent: QTreeWidgetItem,
        key: str,
        name: str,
        saved: str | None = None,
        one_off: bool = False,
        shift_days: int = 0,
    ):
        item = QTreeWidgetItem([name, key, "", "", str(shift_days) if shift_days else ""])
        item.setTextAlignment(SHIFT_COLUMN, Qt.AlignmentFlag.AlignCenter)
        item.setData(0, SAVED_KEY_ROLE, saved)
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable | Qt.ItemFlag.ItemIsUserCheckable)
        item.setCheckState(3, Qt.CheckState.Checked if one_off else Qt.CheckState.Unchecked)
        parent.addChild(item)
        return item

    def _selected_group(self) -> QTreeWidgetItem | None:
        item = self.tree.currentItem()
        if item is None:
            return None
        return item.parent() or item

    def _add_group(self) -> None:
        taken = {self.tree.topLevelItem(i).text(1) for i in range(self.tree.topLevelItemCount())}
        name = self.tr("New group")
        item = self._group_item(unique(slugify(name), taken), name, False, saved=False)
        self.tree.setCurrentItem(item)
        self.tree.editItem(item, 0)
        self._set_dirty(True)

    def _add_category(self) -> None:
        group = self._selected_group()
        if group is None:
            return
        taken = {group.child(i).text(1) for i in range(group.childCount())}
        name = self.tr("New category")
        item = self._category_item(group, unique(slugify(name), taken), name)
        group.setExpanded(True)
        self.tree.setCurrentItem(item)
        self.tree.editItem(item, 0)
        self._set_dirty(True)

    def _move(self, delta: int) -> None:
        item = self.tree.currentItem()
        if item is None:
            return
        parent = item.parent()
        if parent is None:
            idx = self.tree.indexOfTopLevelItem(item)
            new = idx + delta
            if 0 <= new < self.tree.topLevelItemCount():
                expanded = item.isExpanded()
                self.tree.takeTopLevelItem(idx)
                self.tree.insertTopLevelItem(new, item)
                item.setExpanded(expanded)
        else:
            idx = parent.indexOfChild(item)
            new = idx + delta
            if 0 <= new < parent.childCount():
                parent.takeChild(idx)
                parent.insertChild(new, item)
        self.tree.setCurrentItem(item)
        self._set_dirty(True)

    def move_item(self, item: QTreeWidgetItem, target: QTreeWidgetItem, position: Drop) -> None:
        """Drop ``item`` relative to ``target`` (the row under the cursor).

        Groups only move between groups. A category dropped on a group goes to its end,
        or to its start when dropped just below an expanded group; dropped above or
        below a category it goes next to it, also in another group.
        """
        above = position == Drop.AboveItem
        if item.parent() is None:  # group
            group = target.parent() or target
            if group is item:
                return
            expanded = item.isExpanded()
            self.tree.takeTopLevelItem(self.tree.indexOfTopLevelItem(item))
            index = self.tree.indexOfTopLevelItem(group)
            below = not above or target is not group
            self.tree.insertTopLevelItem(index + 1 if below else index, item)
            item.setExpanded(expanded)
        else:  # category
            if target.parent() is None:
                if above:
                    index = self.tree.indexOfTopLevelItem(target)
                    if index == 0:
                        return
                    parent = self.tree.topLevelItem(index - 1)  # end of the previous group
                    index = parent.childCount()
                elif position == Drop.BelowItem and target.isExpanded():
                    parent, index = target, 0
                else:
                    parent, index = target, target.childCount()
            else:
                parent = target.parent()
                index = parent.indexOfChild(target) + (0 if above else 1)
            old_parent = item.parent()
            if old_parent is parent and parent.indexOfChild(item) < index:
                index -= 1  # the item itself leaves a gap
            old_parent.removeChild(item)
            parent.insertChild(index, item)
            if old_parent is not parent:
                self.tree.blockSignals(True)
                item.setText(1, unique(item.text(1), self._sibling_keys(item)))
                self.tree.blockSignals(False)
                parent.setExpanded(True)
        self.tree.setCurrentItem(item)
        self._set_dirty(True)

    def _remove(self) -> None:
        item = self.tree.currentItem()
        if item is None:
            return
        if item.parent() is None:
            self.tree.takeTopLevelItem(self.tree.indexOfTopLevelItem(item))
        else:
            item.parent().removeChild(item)
        self._set_dirty(True)

    def to_config(self) -> CategoriesConfig:
        # Targets and the budget level are edited in the budget view; keep them.
        group_defs = {g.key: g for g in self.app.settings.categories.groups}
        saved_cats = {
            f"{g.key}.{c.key}": c for g in self.app.settings.categories.groups for c in g.categories
        }
        groups = []
        for i in range(self.tree.topLevelItemCount()):
            g = self.tree.topLevelItem(i)
            saved_group = g.data(0, SAVED_KEY_ROLE)
            cats = []
            for j in range(g.childCount()):
                item = g.child(j)
                # targets belong to the category, also when its key or group changes
                saved = saved_cats.get(item.data(0, SAVED_KEY_ROLE))
                cats.append(
                    CategoryDef(
                        key=item.text(1),
                        name=item.text(0).strip(),
                        target=saved.target if saved else None,
                        one_off=item.checkState(3) == Qt.CheckState.Checked,
                        report_shift_days=int(item.text(SHIFT_COLUMN) or 0),
                    )
                )
            income = g.checkState(2) == Qt.CheckState.Checked
            old = group_defs.get(saved_group)
            by_group = old is not None and old.budget_by_group and not income
            groups.append(
                GroupDef(
                    key=g.text(1),
                    name=g.text(0).strip(),
                    income=income,
                    budget="group" if by_group else "category",
                    target=old.target if by_group else None,
                    categories=cats,
                )
            )
        return CategoriesConfig(groups=groups)

    def renames(self) -> KeyRenames:
        """Keys of saved groups and categories that were changed in the editor."""
        renames = KeyRenames()
        for i in range(self.tree.topLevelItemCount()):
            g = self.tree.topLevelItem(i)
            saved_group = g.data(0, SAVED_KEY_ROLE)
            if saved_group is not None and g.text(1) != saved_group:
                renames.groups[saved_group] = g.text(1)
            for j in range(g.childCount()):
                c = g.child(j)
                old, new = c.data(0, SAVED_KEY_ROLE), f"{g.text(1)}.{c.text(1)}"
                if old is not None and old != new:  # key, group key or group changed
                    renames.categories[old] = new
        return renames

    def save(self) -> None:
        try:
            renames = self.renames()
            if renames:
                with self.app.session() as s:
                    check_key_renames(s, renames.groups, renames.categories)
            config_edit.save_categories(self.app.settings, self.to_config(), renames)
            self.app.sync_config(renames)
        except Exception as e:
            show_error(self, self.tr("Save categories"), e)
            return
        self.load()
        self.saved.emit()


# --- rules ---------------------------------------------------------------------------


@dataclass
class RuleDraft:
    id: str = ""
    category: str = ""
    counterparty: str = ""
    merchant: str = ""
    purpose: str = ""
    booking_type: str = ""
    details: str = ""
    account: str = ""
    amount_min: str = ""
    amount_max: str = ""

    TEXT_FIELDS = ("counterparty", "merchant", "purpose", "booking_type", "details", "account")

    @classmethod
    def from_rule(cls, r: RuleDef) -> RuleDraft:
        m = r.match
        num = lambda v: "" if v is None else f"{v:g}"  # noqa: E731
        return cls(
            id=r.id,
            category=r.category,
            **{f: getattr(m, f) or "" for f in cls.TEXT_FIELDS},
            amount_min=num(m.amount_min),
            amount_max=num(m.amount_max),
        )

    def to_rule(self) -> RuleDef:
        def num(text: str) -> float | None:
            text = text.strip().replace(",", ".")
            return float(text) if text else None

        match = {f: getattr(self, f).strip() or None for f in self.TEXT_FIELDS}
        for f in self.TEXT_FIELDS:
            if match[f]:
                re.compile(match[f])  # raises re.error with a useful message
        return RuleDef(
            id=self.id.strip(),
            category=self.category,
            match=RuleMatch(
                **match, amount_min=num(self.amount_min), amount_max=num(self.amount_max)
            ),
        )


class RuleEditor(QWidget):
    saved = Signal()
    #: unsaved changes appeared (True) or were saved / discarded (False)
    dirty_changed = Signal(bool)
    #: show these bookings (ids) on the bookings page, labelled
    show_bookings = Signal(list, str)

    def __init__(self, app: App, parent=None) -> None:
        super().__init__(parent)
        self.app = app
        self.drafts: list[RuleDraft] = []
        self._loading = False
        # what the rules look at in each booking, loaded when the page is shown; the hits
        # of every rule are counted again from it after each change (#49)
        self._samples: list[config_edit.RuleSample] | None = None
        self.hits: list[config_edit.RuleHits] = []
        self._hits_stale = True  # a rule changed since the hits were counted

        self.search = QLineEdit(placeholderText=self.tr("Search rules (id, category, pattern)…"))
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._apply_search)
        self.list = QListWidget()
        self.list.currentRowChanged.connect(self._show_draft)
        new_rule = QPushButton(self.tr("New rule"))
        new_rule.setToolTip(shortcuts.tip(self.tr("New rule"), "item_new"))
        new_rule.clicked.connect(lambda: self._add())
        list_buttons = action_bar(
            [new_rule], icon_buttons(self, self.tr("Up"), self.tr("Down"), self.tr("Remove"))
        )
        shortcuts.bind(
            "item_new", self, lambda: self._add(), Qt.ShortcutContext.WidgetWithChildrenShortcut
        )
        bind_list_keys(self, self.list)
        shortcuts.bind(
            "item_edit", self.list, self._edit_pattern, Qt.ShortcutContext.WidgetShortcut
        )

        self.inputs: dict[str, QLineEdit] = {}
        self.category = QComboBox()
        form = QFormLayout()
        self.inputs["id"] = QLineEdit()
        form.addRow(self.tr("Rule id"), self.inputs["id"])
        form.addRow(self.tr("Category"), self.category)
        placeholders = {
            "counterparty": self.tr("Regex on counterparty, e.g. (?i)vodafone"),
            "merchant": self.tr("Regex on card/PayPal merchant, e.g. (?i)rewe|aldi"),
            "purpose": self.tr("Regex on purpose text"),
            "booking_type": self.tr("Regex on booking type, e.g. ^Gehalt/Rente$"),
            "details": self.tr("Regex on PayPal payee and article"),
            "account": self.tr("Regex on own account name"),
            "amount_min": self.tr("EUR, negative = outgoing"),
            "amount_max": self.tr("EUR, negative = outgoing"),
        }
        labels = {
            "counterparty": self.tr("Counterparty"),
            "merchant": self.tr("Merchant"),
            "purpose": self.tr("Purpose"),
            "booking_type": self.tr("Booking type"),
            "details": self.tr("Payment details"),
            "account": self.tr("Account"),
            "amount_min": self.tr("Amount from"),
            "amount_max": self.tr("Amount to"),
        }
        for name, label in labels.items():
            edit = QLineEdit(placeholderText=placeholders[name])
            self.inputs[name] = edit
            if name not in ("amount_min", "amount_max"):
                regex_help.attach(edit)
            form.addRow(label, edit)
        for edit in self.inputs.values():
            edit.textEdited.connect(self._form_changed)
        self.category.currentIndexChanged.connect(self._form_changed)

        self.match_info = banner(QLabel(), "info")  # kind set by the match result
        self.match_info.linkActivated.connect(lambda _link: self._show_matches())
        self._match_timer = QTimer(self, singleShot=True, interval=300)
        self._match_timer.timeout.connect(self._update_match_info)

        form_box = self.form_box = DetailCard(self.tr("Rule"))
        form_layout = form_box.body
        form_layout.addLayout(form)
        form_layout.addWidget(self.match_info)
        form_layout.addStretch(1)

        # save / revert at the bottom of the card, as for the accounts
        self.save_button = primary(QPushButton(self.tr("Save rules")))
        self.save_button.clicked.connect(self.save)
        self.revert_button = QPushButton(self.tr("Revert"))
        self.revert_button.clicked.connect(self.load)
        bottom = QHBoxLayout()
        bottom.addStretch(1)
        bottom.addWidget(self.revert_button)
        bottom.addWidget(self.save_button)
        form_layout.addLayout(bottom)
        order = banner(QLabel(self.tr("Rules are checked top to bottom; the first match wins.")))

        left = QVBoxLayout()
        left.addWidget(order)
        left.addWidget(self.search)
        left.addWidget(self.list, 1)
        left.addLayout(list_buttons)
        layout = QHBoxLayout(self)
        layout.addLayout(left, 2)
        layout.addWidget(form_box, 3)
        self.load()

    # -- loading -----------------------------------------------------------------

    def _fill_categories(self) -> None:
        self.category.blockSignals(True)
        self.category.clear()
        for g in self.app.settings.categories.groups:
            self.category.addItem(g.name, None)
            self.category.model().item(self.category.count() - 1).setEnabled(False)
            for c in g.categories:
                self.category.addItem(f"    {c.name}", f"{g.key}.{c.key}")
        self.category.blockSignals(False)

    def _category_name(self, key: str) -> str:
        idx = self.category.findData(key)
        return self.category.itemText(idx).strip() if idx >= 0 else key

    def load(self) -> None:
        self._fill_categories()
        self.drafts = [RuleDraft.from_rule(r) for r in self.app.settings.rules.rules]
        self._rebuild_list(0)
        self._set_dirty(False)

    def bookings_changed(self) -> None:
        """Bookings were edited, imported or recategorized: the hits are counted again,
        now if the page is visible, else when it is shown. (They were thrown away on every
        hide: 0.64 s on every visit with ten years of bookings, #57.)"""
        self._samples = None
        if self.isVisible():
            self._recount()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._recount()

    def _recount(self) -> None:
        """The hits again, if the bookings were loaded anew."""
        if self._samples is None:
            self._hits_stale = True
            self._update_hits()
            self._update_match_info()

    def _rebuild_list(self, select: int) -> None:
        self.list.blockSignals(True)
        self.list.clear()
        for d in self.drafts:
            self.list.addItem(QListWidgetItem(self._item_text(d)))
        self.list.blockSignals(False)
        self._update_hits()
        self._apply_search()
        if self.drafts:
            self.list.setCurrentRow(max(0, min(select, len(self.drafts) - 1)))
        else:
            self._show_draft(-1)

    def _item_text(self, d: RuleDraft) -> str:
        return f"{d.id or '?'}  →  {self._category_name(d.category)}"

    @staticmethod
    def _preview_rule(d: RuleDraft) -> RuleDef:
        """The draft as a rule, also while its id or category is still empty."""
        rule = RuleDraft(**{f.name: getattr(d, f.name) for f in fields(d)})
        rule.id = rule.id or "preview"
        rule.category = rule.category or "preview.preview"
        return rule.to_rule()

    def _update_hits(self) -> None:
        """Count the hits of every rule (in the list's order) and show them in the list:
        the bookings each rule categorizes; grey when none (obsolete, or every match is
        taken by a rule above it)."""
        if self._samples is None:
            with self.app.session() as s:
                self._samples = config_edit.rule_samples(s)
        rules = []
        for d in self.drafts:
            try:
                rules.append(self._preview_rule(d))
            except Exception:  # an invalid pattern: shown in the form
                rules.append(None)
        self.hits = config_edit.rule_hits(self._samples, rules)
        self._hits_stale = False
        grey = self.palette().color(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text)
        for row, (d, h) in enumerate(zip(self.drafts, self.hits, strict=True)):
            item = self.list.item(row)
            if item is None:
                continue
            item.setText(f"{self._item_text(d)}   ·   {h.effective}")
            item.setData(Qt.ItemDataRole.ForegroundRole, grey if not h.effective else None)
            tip = self.tr("Categorizes %1 bookings.").replace("%1", str(h.effective))
            if h.taken:
                tip += " " + (
                    self.tr("Matches %1; %2 of them are taken by rules above it.")
                    .replace("%1", str(h.matching))
                    .replace("%2", str(h.taken))
                )
            item.setToolTip(tip)

    def _apply_search(self) -> None:
        """Hide the rules that do not contain the search text (id, category, patterns)."""
        needle = self.search.text().strip().casefold()
        for row, d in enumerate(self.drafts):
            item = self.list.item(row)
            if item is None:
                continue
            haystack = " ".join(
                [d.id, d.category, self._category_name(d.category)]
                + [getattr(d, f) for f in RuleDraft.TEXT_FIELDS]
            ).casefold()
            item.setHidden(bool(needle) and needle not in haystack)

    def _set_dirty(self, dirty: bool) -> None:
        self.save_button.setEnabled(dirty)
        self.revert_button.setEnabled(dirty)
        self.dirty_changed.emit(dirty)

    # -- editing -----------------------------------------------------------------

    def _current(self) -> RuleDraft | None:
        row = self.list.currentRow()
        return self.drafts[row] if 0 <= row < len(self.drafts) else None

    def _show_draft(self, row: int) -> None:
        self._loading = True
        d = self.drafts[row] if 0 <= row < len(self.drafts) else None
        for name, edit in self.inputs.items():
            edit.setEnabled(d is not None)
            edit.setText(getattr(d, name) if d else "")
        self.category.setEnabled(d is not None)
        self.category.setCurrentIndex(self.category.findData(d.category) if d else -1)
        self.form_box.set_heading(d.id if d else "")
        self._loading = False
        self._match_timer.start()

    def _form_changed(self) -> None:
        if self._loading:
            return
        d = self._current()
        if d is None:
            return
        for name, edit in self.inputs.items():
            setattr(d, name, edit.text())
        d.category = self.category.currentData() or ""
        self.list.currentItem().setText(self._item_text(d))  # the count follows the timer
        self._hits_stale = True
        self.form_box.set_heading(d.id)
        self._set_dirty(True)
        self._match_timer.start()

    def _update_match_info(self) -> None:
        d = self._current()
        if d is None:
            self.match_info.clear()
            self.match_info.hide()
            return
        self.match_info.show()
        try:
            self._preview_rule(d)  # an invalid rule: say why
        except Exception as e:
            self.match_info.setText(html.escape(self.tr("Invalid rule: %1").replace("%1", str(e))))
            banner(self.match_info, "error")
            if self._hits_stale:
                self._update_hits()
            return
        if self._hits_stale:
            self._update_hits()
        h = self._current_hits()
        if h is None:
            self.match_info.hide()
            return
        text = "✓ " + (
            self.tr("Matches %1 bookings (%2 currently uncategorized).")
            .replace("%1", str(h.matching))
            .replace("%2", str(h.uncategorized))
        )
        if h.taken:
            text += " " + self.tr("%1 of them are taken by rules above it.").replace(
                "%1", str(h.taken)
            )
        if h.matching:
            text += " <a href='#'>" + self.tr("Show bookings") + "</a>"
        self.match_info.setText(text)
        banner(self.match_info, "success" if h.effective else "info")

    def _current_hits(self) -> config_edit.RuleHits | None:
        row = self.list.currentRow()
        return self.hits[row] if 0 <= row < len(self.hits) else None

    def _show_matches(self) -> None:
        """The bookings the current rule matches, on the bookings page."""
        d, h = self._current(), self._current_hits()
        if d is None or h is None or not h.matching:
            return
        label = self.tr("bookings matching the rule “%1”").replace("%1", d.id)
        self.show_bookings.emit(list(h.ids), label)

    def _add(self, draft: RuleDraft | None = None) -> None:
        draft = draft or RuleDraft()
        taken = {d.id for d in self.drafts}
        draft.id = unique(draft.id or "neue_regel", taken)
        row = self.list.currentRow() + 1 if self.list.currentRow() >= 0 else 0
        self.drafts.insert(row, draft)
        self._rebuild_list(row)
        self._set_dirty(True)
        self.inputs["id"].setFocus()

    def _move(self, delta: int) -> None:
        row = self.list.currentRow()
        new = row + delta
        if row < 0 or not 0 <= new < len(self.drafts):
            return
        self.drafts[row], self.drafts[new] = self.drafts[new], self.drafts[row]
        self._rebuild_list(new)
        self._set_dirty(True)

    def _edit_pattern(self) -> None:
        """F2: the first pattern of the rule (the counterparty one if it has none)."""
        names = ("counterparty", "merchant", "purpose", "booking_type", "details", "account")
        name = next((n for n in names if self.inputs[n].text()), "counterparty")
        self.inputs[name].setFocus()
        self.inputs[name].selectAll()

    def _remove(self) -> None:
        row = self.list.currentRow()
        if row >= 0:
            del self.drafts[row]
            self._rebuild_list(row)
            self._set_dirty(True)

    def prefill_from(self, row: TransactionRow) -> None:
        """Start a new rule matching the given booking (inserted at the top)."""
        if row.details:  # paid via PayPal: the payee there names the shop
            source, field = row.details.split(" · ")[0], "details"
        else:
            source = row.merchant or row.counterparty
            field = "merchant" if row.merchant else "counterparty"
        pattern = "(?i)" + re.escape(source.strip())
        single = row.splits[0] if len(row.splits) == 1 else None
        category = single.category_key if single and single.category_key else ""
        draft = RuleDraft(id=slugify(source)[:40], category=category, **{field: pattern})
        self.list.setCurrentRow(-1)
        self._add(draft)

    def save(self) -> None:
        try:
            rules = [d.to_rule() for d in self.drafts]
            config_edit.save_rules(self.app.settings, rules)
        except Exception as e:
            show_error(self, self.tr("Save rules"), e)
            return
        self._set_dirty(False)
        answer = QMessageBox.question(
            self,
            self.tr("Rules saved"),
            self.tr(
                "Re-run categorization for all bookings now? Manually categorized bookings "
                "are not changed."
            ),
        )
        if answer == QMessageBox.StandardButton.Yes:
            with self.app.session() as s:
                stats = recategorize_all(s, self.app.settings)
            QMessageBox.information(self, self.tr("Categorization"), str(stats))
        self.saved.emit()


class ConfigView(QWidget):
    """Tab combining the category and rule editors."""

    changed = Signal()

    def __init__(self, app: App, parent=None) -> None:
        super().__init__(parent)
        self.categories = CategoryEditor(app)
        self.rules = RuleEditor(app)
        self.categories.saved.connect(self.rules.load)
        self.categories.saved.connect(self.changed)
        self.rules.saved.connect(self.changed)
        self.show_bookings = self.rules.show_bookings  # (ids, label) for the bookings page

        cat_box = QGroupBox(self.tr("Categories"))
        QVBoxLayout(cat_box).addWidget(self.categories)
        rule_box = QGroupBox(self.tr("Rules"))
        QVBoxLayout(rule_box).addWidget(self.rules)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(cat_box)
        splitter.addWidget(rule_box)
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 3)
        splitter.setSizes([2000, 3000])  # 2 : 3 from the start, whatever the contents ask
        layout = QVBoxLayout(self)
        layout.addWidget(page_title(self.tr("Categories & rules")))
        layout.addWidget(splitter, 1)
        self.setFocusProxy(self.categories.tree)  # keyboard focus when shown
        # Ctrl+PgDn / PgUp: to the other list (#67)
        for index in (0, 1):
            shortcuts.bind(
                "config_pane",
                self,
                self.other_pane,
                Qt.ShortcutContext.WidgetWithChildrenShortcut,
                index=index,
            )

    def focus_regions(self) -> list[QWidget]:
        """F6: categories, rules, the selected rule."""
        return [self.categories.tree, self.rules.list, self.rules.inputs["id"]]

    def other_pane(self) -> None:
        """Focus from the categories to the rules and back."""
        if self.rules.isAncestorOf(self.focusWidget() or self):
            self.categories.tree.setFocus()
        else:
            self.rules.list.setFocus()

    def has_unsaved_changes(self) -> bool:
        return self.categories.save_button.isEnabled() or self.rules.save_button.isEnabled()
