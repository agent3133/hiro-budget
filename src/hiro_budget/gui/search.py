"""Search in the title bar (Ctrl+K): pages, commands, accounts, categories, bookings.

The results open in a list below the box that never takes the keyboard focus: typing
goes on in the box, Up / Down / Return / Esc are passed to the list.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from PySide6.QtCore import QEvent, QPoint, QRect, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QPainter
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QStyle,
    QStyledItemDelegate,
    QVBoxLayout,
    QWidget,
)

from hiro_budget.gui import theme

HIT_ROLE = Qt.ItemDataRole.UserRole + 1
HEADER_ROLE = Qt.ItemDataRole.UserRole + 2


@dataclass
class Hit:
    """One result: shown as ``text`` with ``detail`` on the right; ``run`` opens it."""

    section: str
    text: str
    detail: str
    run: Callable[[], None]


def match_rank(text: str, query: str) -> int | None:
    """0: the text starts with the query, 1: a word does, 2: it is inside; None: no."""
    t, q = text.casefold(), query.casefold().strip()
    if not q:
        return None
    if t.startswith(q):
        return 0
    if any(word.startswith(q) for word in t.replace("›", " ").split()):
        return 1
    return 2 if q in t else None


def best(items: list[tuple[str, object]], query: str, limit: int) -> list[object]:
    """The values whose text matches, best matches first (stable within a rank)."""
    ranked = [
        (r, i, v) for i, (text, v) in enumerate(items) if (r := match_rank(text, query)) is not None
    ]
    ranked.sort(key=lambda x: (x[0], x[1]))
    return [v for _, _, v in ranked[:limit]]


class HitDelegate(QStyledItemDelegate):
    """A result with its detail (shortcut, amount, ...) on the right; section captions
    in small capitals."""

    def sizeHint(self, option, index):
        size = super().sizeHint(option, index)
        size.setHeight(24 if index.data(HEADER_ROLE) else 30)
        return size

    def paint(self, painter, option, index):
        t = theme.tokens()
        palette = option.palette
        text_color = QColor(t["text"]) if t else palette.text().color()
        muted = QColor(t["muted"]) if t else palette.placeholderText().color()
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = option.rect.adjusted(6, 1, -6, -1)
        if index.data(HEADER_ROLE):
            font = QFont(option.font)
            font.setPointSizeF(font.pointSizeF() * 0.85)
            font.setBold(True)
            font.setCapitalization(QFont.Capitalization.AllUppercase)
            painter.setFont(font)
            painter.setPen(muted)
            painter.drawText(
                r.adjusted(6, 4, 0, 0), Qt.AlignmentFlag.AlignVCenter, index.data() or ""
            )
            painter.restore()
            return
        if option.state & QStyle.StateFlag.State_Selected:
            selected = QColor(t["accent_soft"]) if t else palette.highlight().color()
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(selected)
            painter.drawRoundedRect(r, 5, 5)
            if not t:
                text_color = muted = palette.highlightedText().color()
        hit: Hit = index.data(HIT_ROLE)
        inner = r.adjusted(10, 0, -10, 0)
        painter.setFont(option.font)
        detail_width = 0
        if hit.detail:
            painter.setPen(muted)
            metrics = painter.fontMetrics()
            detail, detail_width = hit.detail, metrics.horizontalAdvance(hit.detail) + 2
            if detail_width > inner.width() // 2:  # only when it really does not fit
                detail_width = inner.width() // 2
                detail = metrics.elidedText(detail, Qt.TextElideMode.ElideLeft, detail_width)
            painter.drawText(
                inner, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, detail
            )
        painter.setPen(text_color)
        text_rect = QRect(
            inner.left(), inner.top(), inner.width() - detail_width - 16, inner.height()
        )
        painter.drawText(
            text_rect,
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
            painter.fontMetrics().elidedText(
                hit.text, Qt.TextElideMode.ElideRight, text_rect.width()
            ),
        )
        painter.restore()


class ResultList(QFrame):
    """The open list below the search box (a window of its own, without focus)."""

    def __init__(self, box: SearchBox) -> None:
        super().__init__(
            box.window(),
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowDoesNotAcceptFocus,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setObjectName("searchResults")
        self.box = box
        self.list = QListWidget()
        self.list.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.list.setFrameShape(QFrame.Shape.NoFrame)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.list.setItemDelegate(HitDelegate(self.list))
        self.list.setMouseTracking(True)
        self.list.itemClicked.connect(lambda item: box.run(item))
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.addWidget(self.list)
        self.restyle()

    def restyle(self) -> None:
        t = theme.tokens()
        if t is None:
            ground, border = "palette(base)", "rgba(128, 128, 128, 0.45)"
        else:
            ground, border = t["input"], t["border"]
        self.setStyleSheet(
            f"QFrame#searchResults {{ background: {ground}; border: 1px solid {border};"
            " border-radius: 8px; }"
            "QFrame#searchResults QListWidget { background: transparent; border: none; }"
        )

    def show_hits(self, hits: list[Hit]) -> None:
        self.list.clear()
        section = None
        for hit in hits:
            if hit.section != section:
                section = hit.section
                header = QListWidgetItem(section)
                header.setData(HEADER_ROLE, True)
                header.setFlags(Qt.ItemFlag.NoItemFlags)
                self.list.addItem(header)
            item = QListWidgetItem(hit.text)
            item.setData(HIT_ROLE, hit)
            self.list.addItem(item)
        if not hits:
            self.hide()
            return
        self.move_selection(1)
        self.place()
        self.show()
        self.raise_()

    def place(self) -> None:
        """Below the box, as wide as it (at least 560 px), as high as the results."""
        box = self.box
        width = max(box.width(), 560)
        rows = sum(self.list.sizeHintForRow(i) for i in range(self.list.count()))
        height = min(rows + 14, 520)
        top_left = box.mapToGlobal(QPoint((box.width() - width) // 2, box.height() + 4))
        self.setGeometry(QRect(top_left, QSize(width, height)))

    def move_selection(self, step: int) -> None:
        """The next result up or down (captions are skipped)."""
        n = self.list.count()
        row = self.list.currentRow()
        for _ in range(n):
            row = (row + step) % n if row >= 0 else (0 if step > 0 else n - 1)
            if self.list.item(row).data(HIT_ROLE) is not None:
                self.list.setCurrentRow(row)
                return

    def current(self) -> QListWidgetItem | None:
        item = self.list.currentItem()
        return item if item is not None and item.data(HIT_ROLE) is not None else None


class SearchBox(QLineEdit):
    """The box; ``find(query)`` returns the hits, grouped by section. Made with the
    main window as parent (the list belongs to it), then put into the title bar."""

    def __init__(self, find: Callable[[str], list[Hit]], window: QWidget) -> None:
        super().__init__(window)
        self.find = find
        self.setObjectName("titleSearch")
        self.setClearButtonEnabled(True)
        self.setMinimumWidth(260)
        self.setMaximumWidth(520)
        self.results = ResultList(self)
        self._timer = QTimer(self, singleShot=True, interval=120)  # not on every key
        self._timer.timeout.connect(self.update_results)
        self.textEdited.connect(lambda _t: self._timer.start())
        self.window().installEventFilter(self)  # moved or left: close the list

    def update_results(self) -> None:
        query = self.text().strip()
        self.results.show_hits(self.find(query) if query else [])

    def run(self, item: QListWidgetItem) -> None:
        hit: Hit = item.data(HIT_ROLE)
        self.close_results()
        self.clear()
        hit.run()

    def close_results(self) -> None:
        self._timer.stop()
        self.results.hide()

    def keyPressEvent(self, event) -> None:
        key = event.key()
        visible = self.results.isVisible()
        if key in (Qt.Key.Key_Down, Qt.Key.Key_Up) and visible:
            self.results.move_selection(1 if key == Qt.Key.Key_Down else -1)
        elif key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if self._timer.isActive():  # typed fast and pressed Return: search first
                self._timer.stop()
                self.update_results()
            item = self.results.current() if self.results.isVisible() else None
            if item is not None:
                self.run(item)
        elif key == Qt.Key.Key_Escape:
            if visible or self.text():
                self.close_results()
                self.clear()
            else:
                self.leave()
        else:
            super().keyPressEvent(event)

    def leave(self) -> None:
        """Keyboard focus back to the page shown."""
        window = self.window()
        page = getattr(window, "tabs", None)
        if page is not None and page.currentWidget() is not None:
            page.currentWidget().setFocus()

    def focusOutEvent(self, event) -> None:
        super().focusOutEvent(event)
        # a click into the list arrives after the focus left the box
        QTimer.singleShot(150, lambda: None if self.hasFocus() else self.close_results())

    def eventFilter(self, obj, event) -> bool:
        if event.type() in (QEvent.Type.Move, QEvent.Type.Resize, QEvent.Type.WindowDeactivate):
            if event.type() != QEvent.Type.WindowDeactivate or not self.results.underMouse():
                self.close_results()
        return False
