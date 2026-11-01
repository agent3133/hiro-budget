"""Sidebar navigation in the style of Windows 11 (Explorer, Settings).

``NavigationView`` offers the parts of the ``QTabWidget`` API the main window uses
(addTab, currentWidget, setCurrentWidget, currentChanged, ...), so pages stay plain
widgets.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QEvent, QPoint, QRectF, QSize, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontDatabase,
    QIcon,
    QKeySequence,
    QPainter,
    QPen,
    QPixmap,
    QShortcut,
)
from PySide6.QtWidgets import (
    QAbstractButton,
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QStyle,
    QStyledItemDelegate,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from hiro_budget.gui import theme
from hiro_budget.gui.app_icon import ring_pixmap
from hiro_budget.gui.common import amount_color, money, with_keys
from hiro_budget.gui.widgets import caption

GLYPH_ROLE = Qt.ItemDataRole.UserRole + 1
LABEL_ROLE = Qt.ItemDataRole.UserRole + 2
ACCOUNT_ROLE = Qt.ItemDataRole.UserRole + 3

WIDE, COMPACT = 230, 56  # sidebar width with labels / icons only
COMPACT_BELOW = 1400  # narrower (e.g. Full HD laptop at 150 %): icons only

# Codepoints shared by "Segoe Fluent Icons" (Windows 11) and "Segoe MDL2 Assets" (Windows 10)
ICON_OVERVIEW = "\ue80f"  # Home
ICON_BUDGET = "\ue8ef"  # Calculator
ICON_FOLDER = "\ue8b7"  # Folder (where the database is)
ICON_BOOKINGS = "\ue8fd"  # BulletedList
ICON_ACCOUNTS = "\ue825"  # Bank
ICON_REPORTS = "\ue9d2"  # AreaChart
ICON_CATEGORIES = "\ue8ec"  # Tag
ICON_PREVIOUS = "\ue76b"  # ChevronLeft
ICON_NEXT = "\ue76c"  # ChevronRight
ICON_UNDO = "\ue7a7"  # Undo
ICON_REDO = "\ue7a6"  # Redo
ICON_FILE = "\ue8a5"  # Document
ICON_VIEW = "\ue890"  # View
ICON_TOOLS = "\ue90f"  # Repair
ICON_HELP = "\ue897"  # Help
ICON_SETTINGS = "\ue713"  # Setting
ICON_BACK = "\ue72b"  # Back
ICON_FORWARD = "\ue72a"  # Forward
ICON_IMPORT = "\ue8b5"  # Import
ICON_FETCH = "\ue895"  # Sync
ICON_UP = "\ue74a"  # Up
ICON_DOWN = "\ue74b"  # Down
ICON_DELETE = "\ue74d"  # Delete
ICON_SUN = "\ue706"  # Brightness
ICON_MOON = "\ue708"  # QuietHours

STYLE = """
QListWidget#navigation {
    background: transparent;
    border: none;
    outline: 0;
}
QListWidget#navigation::item {
    min-height: 36px;
    padding-left: 10px;
    margin: 1px 6px;
    border-radius: 5px;
    border-left: 3px solid transparent;
    color: palette(text);
}
QListWidget#navigation::item:hover {
    background: rgba(128, 128, 128, 0.12);
}
QListWidget#navigation::item:selected {
    background: rgba(128, 128, 128, 0.20);
    border-left: 3px solid palette(highlight);
    color: palette(text);
}
QTreeWidget#sidebarAccounts {
    background: transparent;
    border: none;
    outline: 0;
}
QTreeWidget#sidebarAccounts::item { padding: 3px 0; }
QToolButton#sidebarFooter {
    border: none; border-radius: 6px; padding: 4px 6px; font-weight: 600;
}
QToolButton#sidebarFooter:hover { background: rgba(128, 128, 128, 0.12); }
QPushButton#sidebarMenu {
    background: transparent; border: none; border-radius: 5px;
    padding: 7px 12px; text-align: left; color: palette(text);
}
QPushButton#sidebarMenu:hover { background: rgba(128, 128, 128, 0.12); }
QPushButton#sidebarMenu:pressed { background: rgba(128, 128, 128, 0.20); }
"""

# Obsidian: the list is a panel; TileDelegate draws the pages as pastel tiles
OBSIDIAN_STYLE = """
QWidget#sidebar {
    background: {panel};
    border-right: 1px solid {border};
}
QListWidget#navigation {
    background: transparent;
    border: none;
    outline: 0;
}
QTreeWidget#sidebarAccounts {
    background: transparent;
    border: none;
    outline: 0;
}
QTreeWidget#sidebarAccounts::item { padding: 3px 0; color: {text}; }
QToolButton#sidebarFooter {
    background: transparent; color: {text};
    border: none; border-top: 1px solid {border}; padding: 8px 6px; font-weight: 600;
}
QToolButton#sidebarFooter:hover { background: {line}; }
QPushButton#sidebarMenu {
    background: transparent; color: {text}; border: none; border-radius: 6px;
    padding: 6px 12px; min-height: 20px; text-align: left;
}
QPushButton#sidebarMenu:hover { background: {line}; }
QPushButton#sidebarMenu:pressed { background: {border}; }
"""


class TileDelegate(QStyledItemDelegate):
    """Obsidian: each page a rounded tile in its pastel colour, the current one with an
    accent frame and bold label."""

    def sizeHint(self, option, index):
        size = super().sizeHint(option, index)
        size.setHeight(34)  # slimmer than the Windows look, as in Obsidian
        return size

    def paint(self, painter, option, index):
        t = theme.tokens()
        pages = t["pages"]
        selected = option.state & QStyle.StateFlag.State_Selected
        hover = option.state & QStyle.StateFlag.State_MouseOver
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(option.rect).adjusted(10, 2, -10, -2)
        painter.setBrush(QColor(pages[index.row() % len(pages)]))
        if selected:
            painter.setPen(QPen(QColor(t["accent"]), 1.5))
        elif hover:
            painter.setPen(QPen(QColor(t["border"]), 1.5))
        else:
            painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(r, 6, 6)
        icon = index.data(Qt.ItemDataRole.DecorationRole)
        text = index.data() or ""
        x = r.left() + 10
        if icon is not None and not icon.isNull():
            if not text:  # compact: the icon in the middle
                x = r.center().x() - 10
            icon.paint(painter, int(x), int(r.center().y() - 10), 20, 20)
            x += 30
        if text:
            font = QFont(option.font)
            font.setBold(bool(selected))
            painter.setFont(font)
            painter.setPen(QColor(t["heading"] if selected else t["text"]))
            painter.drawText(
                QRectF(x, r.top(), r.right() - x - 6, r.height()),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                text,
            )
        painter.restore()


def icon_font() -> str | None:
    families = set(QFontDatabase.families())
    for family in ("Segoe Fluent Icons", "Segoe MDL2 Assets"):
        if family in families:
            return family
    return None


def glyph_icon(glyph: str, color, size: int = 20) -> QIcon:
    """Icon drawn from the Windows icon font; empty where the font is missing."""
    family = icon_font()
    if family is None or not glyph:
        return QIcon()
    scale = 2  # sharp on high-DPI screens
    pixmap = QPixmap(size * scale, size * scale)
    pixmap.setDevicePixelRatio(scale)
    pixmap.fill(Qt.GlobalColor.transparent)
    font = QFont(family)
    font.setPixelSize(round(size * 0.8))
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
    painter.setFont(font)
    painter.setPen(color)
    painter.drawText(QRectF(0, 0, size, size), Qt.AlignmentFlag.AlignCenter, glyph)
    painter.end()
    return QIcon(pixmap)


def sidebar_icon(color, size: int = 18) -> QIcon:
    """A window with its side panel (as in Obsidian and Firefox): shows / hides the labels."""
    scale = 2  # sharp on high-DPI screens
    pixmap = QPixmap(size * scale, size * scale)
    pixmap.setDevicePixelRatio(scale)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    u = size / 16  # drawn on a 16 x 16 grid
    painter.setPen(QPen(QColor(color), 1.3 * u))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawRoundedRect(QRectF(1.5 * u, 2.5 * u, 13 * u, 11 * u), 2.5 * u, 2.5 * u)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(color))
    painter.drawRoundedRect(QRectF(3.6 * u, 4.6 * u, 3 * u, 6.8 * u), 1 * u, 1 * u)
    painter.end()
    return QIcon(pixmap)


def set_glyph(button, glyph: str, fallback: str = "") -> None:
    """Icon from the Windows icon font on a button; ``fallback`` text without the font."""
    icon = glyph_icon(glyph, button.palette().buttonText().color(), 16)
    if icon.isNull():
        if fallback:
            button.setText(fallback + (" " + button.text() if button.text() else ""))
        return
    button.setProperty("glyph", glyph)
    button.setIcon(icon)
    button.setIconSize(QSize(16, 16))


def refresh_glyphs(root: QWidget) -> None:
    """Redraw button icons in the current text colour (after switching light/dark)."""
    for button in root.findChildren(QAbstractButton):
        glyph = button.property("glyph")
        if glyph:
            button.setIcon(glyph_icon(glyph, button.palette().buttonText().color(), 16))


class NavigationView(QWidget):
    currentChanged = Signal(int)
    #: an account in the sidebar list was clicked (its name)
    accountClicked = Signal(str)
    #: the database name at the bottom was clicked
    footerClicked = Signal()
    #: the user collapsed (True) or expanded (False) the sidebar with the button
    collapsedChanged = Signal(bool)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.list = QListWidget()
        self.list.setObjectName("navigation")
        # As narrow as the icons; the width in use is set in resizeEvent.
        self.list.setMinimumWidth(COMPACT)
        self.list.setMaximumWidth(WIDE)
        self.compact = False
        self.narrow = False  # window too narrow for the labels
        self.collapsed: bool | None = None  # the user's choice; None: by window width
        self.list.setIconSize(QSize(20, 20))
        self.list.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.stack = QStackedWidget()
        self._builders: dict[QWidget, Callable[[], QWidget]] = {}  # pages not made yet

        self.toggle = QToolButton()
        self.toggle.setObjectName("sidebarToggle")
        self.toggle.setAutoRaise(True)
        self.toggle.setFixedSize(36, 32)
        self.toggle.setIconSize(QSize(18, 18))
        self.toggle.clicked.connect(self.toggle_collapsed)
        self.toggle.setVisible(icon_font() is not None)  # no icons: the labels must stay
        # (the main window puts the button into its title bar)

        # name of the app above the pages; the accounts with their balances below them;
        # the database at the bottom (all hidden when the sidebar shows icons only)
        self.heading = caption("")
        self.accounts_caption = caption(self.tr("Accounts"))
        self.heading.setContentsMargins(8, 8, 8, 6)
        self.accounts_caption.setContentsMargins(18, 8, 8, 6)
        self.logo = QLabel()  # the app icon in front of the name
        self.logo.setContentsMargins(0, 2, 0, 0)
        self.logo.setPixmap(ring_pixmap(16, self.devicePixelRatioF()))
        self.brand = QWidget()
        brand = QHBoxLayout(self.brand)
        brand.setContentsMargins(18, 0, 0, 0)
        brand.setSpacing(0)
        brand.addWidget(self.logo)
        brand.addWidget(self.heading, 1)
        self.accounts = QTreeWidget()
        self.accounts.setObjectName("sidebarAccounts")
        self.accounts.setColumnCount(2)
        self.accounts.setHeaderHidden(True)
        self.accounts.setRootIsDecorated(False)
        self.accounts.setIndentation(0)
        self.accounts.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.accounts.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.accounts.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.accounts.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.accounts.setMaximumWidth(WIDE)
        self.accounts.setCursor(Qt.CursorShape.PointingHandCursor)
        header = self.accounts.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.accounts.itemClicked.connect(
            lambda item, _col: self.accountClicked.emit(item.data(0, ACCOUNT_ROLE))
        )
        self.footer = QToolButton()
        self.footer.setObjectName("sidebarFooter")
        self.footer.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.footer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.footer.clicked.connect(self.footerClicked)
        self.extras = [self.brand, self.accounts_caption, self.accounts, self.footer]

        self.sidebar = QWidget()
        self.sidebar.setObjectName("sidebar")
        self.sidebar.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        side = self.side = QVBoxLayout(self.sidebar)
        side.setContentsMargins(0, 8, 0, 8)
        side.setSpacing(0)
        side.addWidget(self.brand)
        side.addWidget(self.list)
        side.addWidget(self.accounts_caption)
        accounts_row = QHBoxLayout()
        accounts_row.setContentsMargins(14, 0, 10, 0)  # in line with the captions
        accounts_row.addWidget(self.accounts)
        side.addLayout(accounts_row)
        side.addStretch(1)
        footer_row = QHBoxLayout()
        footer_row.setContentsMargins(8, 4, 8, 0)
        footer_row.addWidget(self.footer)
        side.addLayout(footer_row)
        # the menus at the very bottom (add_menu), as icons only when collapsed
        self.menu_buttons: list[QPushButton] = []
        self.menu_box = QVBoxLayout()
        self.menu_box.setContentsMargins(8, 4, 8, 0)
        self.menu_box.setSpacing(1)
        side.addLayout(self.menu_box)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addWidget(self.sidebar)
        layout.addWidget(self.stack, 1)
        self.list.currentRowChanged.connect(self._row_changed)
        self.restyle()
        self._update_toggle()

    # -- QTabWidget-like API ----------------------------------------------------

    def addTab(self, widget: QWidget, label: str, glyph: str = "") -> int:
        item = QListWidgetItem(label)
        item.setData(GLYPH_ROLE, glyph)
        item.setData(LABEL_ROLE, label)
        item.setIcon(glyph_icon(glyph, self.palette().text().color()))
        item.setSizeHint(QSize(0, 40))
        self.list.addItem(item)
        self._fit_list()
        index = self.stack.addWidget(widget)
        if index == 0:  # selected quietly: a lazy page is made when it is asked for
            self.list.blockSignals(widget in self._builders)
            self.list.setCurrentRow(0)
            self.list.blockSignals(False)
        return index

    def add_lazy_tab(self, build: Callable[[], QWidget], label: str, glyph: str = "") -> int:
        """A page made by ``build`` when it is first shown (or asked for with ``widget``):
        the start of the app only makes the page it opens with."""
        placeholder = QWidget()
        self._builders[placeholder] = build
        return self.addTab(placeholder, label, glyph)

    def is_built(self, index: int) -> bool:
        return self.stack.widget(index) not in self._builders

    def _build(self, index: int) -> QWidget | None:
        placeholder = self.stack.widget(index)
        build = self._builders.pop(placeholder, None)
        if build is None:
            return placeholder
        page = build()
        current = self.stack.currentIndex() == index
        self.stack.insertWidget(index, page)
        self.stack.removeWidget(placeholder)
        placeholder.deleteLater()
        if current:
            self.stack.setCurrentIndex(index)
        return page

    def count(self) -> int:
        return self.stack.count()

    def tabText(self, index: int) -> str:
        return self.list.item(index).data(LABEL_ROLE)

    def indexOf(self, widget: QWidget) -> int:
        return self.stack.indexOf(widget)

    def widget(self, index: int) -> QWidget | None:
        return self._build(index) if 0 <= index < self.stack.count() else None

    def currentIndex(self) -> int:
        return self.stack.currentIndex()

    def setCurrentIndex(self, index: int) -> None:
        self.list.setCurrentRow(index)

    def currentWidget(self) -> QWidget | None:
        return self.widget(self.stack.currentIndex())

    def setCurrentWidget(self, widget: QWidget) -> None:
        self.setCurrentIndex(self.stack.indexOf(widget))

    # -- extras ------------------------------------------------------------------

    def set_heading(self, text: str) -> None:
        """Name of the app above the pages."""
        self.heading.setText(text.upper())

    def set_accounts(self, accounts: list[tuple[str, int]]) -> None:
        """Accounts with their balances (cents) below the pages; a click opens one."""
        self.accounts.clear()
        for name, cents in accounts:
            item = QTreeWidgetItem([name, money(cents)])
            item.setData(0, ACCOUNT_ROLE, name)
            item.setToolTip(0, name)
            item.setTextAlignment(1, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            if cents < 0:
                item.setForeground(1, amount_color(cents))
            self.accounts.addTopLevelItem(item)
        self._fit_accounts()

    def set_footer(self, text: str, tooltip: str) -> None:
        """The database at the bottom of the sidebar."""
        self.footer.setText(text)
        self.footer.setToolTip(tooltip)
        set_glyph(self.footer, ICON_FOLDER)

    def add_menu(self, menu: QMenu, glyph: str) -> QPushButton:
        """A menu as an entry at the bottom; it opens to the right (also with Alt+letter)."""
        label = menu.title().replace("&", "")
        button = QPushButton(label)
        button.setObjectName("sidebarMenu")
        button.setProperty("label", label)
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        button.setMaximumWidth(WIDE)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        set_glyph(button, glyph)
        button.clicked.connect(lambda: self._open_menu(menu, button))
        mnemonic = QKeySequence.mnemonic(menu.title())
        if not mnemonic.isEmpty():
            QShortcut(mnemonic, self, lambda: self._open_menu(menu, button))
        self.menu_buttons.append(button)
        self.menu_box.addWidget(button)
        self._label_menu(button)
        return button

    def _open_menu(self, menu: QMenu, button: QPushButton) -> None:
        """Beside the sidebar, its bottom in line with the entry (the lowest ones are
        near the screen edge)."""
        size = menu.sizeHint()
        menu.exec(button.mapToGlobal(QPoint(button.width() + 6, button.height() - size.height())))

    def _label_menu(self, button: QPushButton) -> None:
        label = button.property("label")
        has_icon = not button.icon().isNull()
        button.setText("" if self.compact and has_icon else label)
        button.setToolTip(label if self.compact else "")

    def _fit_list(self) -> None:
        """Pages list as high as its rows, so the accounts follow right below."""
        rows = sum(self.list.sizeHintForRow(i) for i in range(self.list.count()))
        self.list.setFixedHeight(rows + 2 * self.list.frameWidth() + 4)

    def _fit_accounts(self) -> None:
        rows = sum(
            self.accounts.sizeHintForRow(i) for i in range(self.accounts.topLevelItemCount())
        )
        self.accounts.setFixedHeight(rows + 2 * self.accounts.frameWidth() + 4)

    def restyle(self) -> None:
        """Look of the sidebar for the theme in use (also after switching it)."""
        t = theme.tokens()
        if t is None:
            self.side.setContentsMargins(0, 8, 0, 8)
            self.sidebar.setStyleSheet(STYLE)
            self.list.setItemDelegate(QStyledItemDelegate(self.list))
        else:  # a full-height panel from the menu bar to the status bar
            self.side.setContentsMargins(0, 6, 1, 0)  # 1: the border on the right
            style = OBSIDIAN_STYLE
            for key in ("panel", "line", "border"):
                style = style.replace("{" + key + "}", t[key])
            self.sidebar.setStyleSheet(style)
            self.list.setItemDelegate(TileDelegate(self.list))
        self._paint_toggle()
        self._fit_list()  # the tiles have another height
        for i in range(self.accounts.topLevelItemCount()):  # amounts in the theme's red
            item = self.accounts.topLevelItem(i)
            if item.foreground(1).style() != Qt.BrushStyle.NoBrush:
                item.setForeground(1, amount_color(-1))

    def toggle_collapsed(self) -> None:
        """Labels off or on (button, Ctrl+B); kept as the user's choice."""
        self.set_collapsed(not self.compact)
        self.collapsedChanged.emit(self.compact)

    def set_collapsed(self, collapsed: bool | None) -> None:
        """The user's choice; None: icons only when the window is narrow."""
        self.collapsed = collapsed
        self._update_compact()

    def _update_compact(self) -> None:
        if icon_font() is None:  # without icons the labels are all there is
            return
        self.set_compact(self.narrow if self.collapsed is None else self.collapsed)

    # -- internals ----------------------------------------------------------------

    def _row_changed(self, row: int) -> None:
        if row < 0:
            return
        self._build(row)
        self.stack.setCurrentIndex(row)
        self.currentChanged.emit(row)

    def resizeEvent(self, event) -> None:
        # Narrow window (laptop): icons only, like Windows Settings; the page gets the room
        # (unless the user chose with the button).
        self.narrow = event.size().width() < COMPACT_BELOW
        self._update_compact()
        super().resizeEvent(event)

    def set_compact(self, compact: bool) -> None:
        if compact == self.compact:
            return
        self.compact = compact
        self.list.setMaximumWidth(COMPACT if compact else WIDE)
        for i in range(self.list.count()):
            item = self.list.item(i)
            label = item.data(LABEL_ROLE)
            item.setText("" if compact else label)
            item.setToolTip(label if compact else "")
        for w in self.extras:
            w.setVisible(not compact)
        for button in self.menu_buttons:
            self._label_menu(button)
        self._update_toggle()

    def _paint_toggle(self) -> None:
        """The panel icon in the theme's muted ink (Obsidian) or the button text colour."""
        t = theme.tokens()
        color = t["muted"] if t else self.toggle.palette().buttonText().color()
        self.toggle.setIcon(sidebar_icon(color))

    def _update_toggle(self) -> None:
        text = self.tr("Expand sidebar") if self.compact else self.tr("Collapse sidebar")
        self.toggle.setToolTip(with_keys(text, "Ctrl+B"))
        self.toggle.setAccessibleName(text)

    def changeEvent(self, event) -> None:
        # Light/dark switched in Windows: redraw the icons in the new text colour.
        if event.type() in (QEvent.Type.PaletteChange, QEvent.Type.ApplicationPaletteChange):
            color = self.palette().text().color()
            for i in range(self.list.count()):
                item = self.list.item(i)
                item.setIcon(glyph_icon(item.data(GLYPH_ROLE), color))
            self._paint_toggle()
        super().changeEvent(event)
