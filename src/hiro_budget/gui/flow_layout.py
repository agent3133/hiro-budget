"""Layout that wraps its widgets onto further lines when the width runs out.

Used for button rows, so that a page stays usable in narrow windows (laptops) instead
of forcing the window to be as wide as all buttons together. Port of Qt's flow layout
example.
"""

from __future__ import annotations

from PySide6.QtCore import QMargins, QPoint, QRect, QSize, Qt
from PySide6.QtWidgets import QLayout, QLayoutItem, QSizePolicy, QStyle, QWidget


class FlowLayout(QLayout):
    def __init__(self, parent: QWidget | None = None, spacing: int = -1) -> None:
        super().__init__(parent)
        self._items: list[QLayoutItem] = []
        self._spacing = spacing
        self.setContentsMargins(QMargins(0, 0, 0, 0))

    # -- QLayout -------------------------------------------------------------------

    def addItem(self, item: QLayoutItem) -> None:
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int) -> QLayoutItem | None:
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index: int) -> QLayoutItem | None:
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self) -> Qt.Orientation:
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return self._arrange(QRect(0, 0, width, 0), apply=False)

    def setGeometry(self, rect: QRect) -> None:
        super().setGeometry(rect)
        self._arrange(rect, apply=True)

    def sizeHint(self) -> QSize:
        """All widgets on one line."""
        width = sum(i.sizeHint().width() for i in self._items)
        width += self._gap(Qt.Orientation.Horizontal) * max(0, len(self._items) - 1)
        height = max((i.sizeHint().height() for i in self._items), default=0)
        m = self.contentsMargins()
        return QSize(width + m.left() + m.right(), height + m.top() + m.bottom())

    def minimumSize(self) -> QSize:
        """As narrow as the widest widget: the rest wraps."""
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        m = self.contentsMargins()
        return size + QSize(m.left() + m.right(), m.top() + m.bottom())

    # -- internals -------------------------------------------------------------------

    def _gap(self, orientation: Qt.Orientation) -> int:
        if self._spacing >= 0:
            return self._spacing
        parent = self.parentWidget() if self.parent() is not None else None
        style = parent.style() if parent is not None else None
        if style is None:
            return 6
        gap = style.layoutSpacing(
            QSizePolicy.ControlType.PushButton,
            QSizePolicy.ControlType.PushButton,
            orientation,
        )
        if gap < 0:
            metric = (
                QStyle.PixelMetric.PM_LayoutHorizontalSpacing
                if orientation == Qt.Orientation.Horizontal
                else QStyle.PixelMetric.PM_LayoutVerticalSpacing
            )
            gap = style.pixelMetric(metric)
        return gap if gap >= 0 else 6

    def _arrange(self, rect: QRect, apply: bool) -> int:
        """Place the items line by line; returns the height needed."""
        m = self.contentsMargins()
        area = rect.adjusted(m.left(), m.top(), -m.right(), -m.bottom())
        x, y, line_height = area.x(), area.y(), 0
        h_gap, v_gap = self._gap(Qt.Orientation.Horizontal), self._gap(Qt.Orientation.Vertical)
        for item in self._items:
            if item.isEmpty():  # hidden widget
                continue
            hint = item.sizeHint()
            if x + hint.width() > area.right() + 1 and line_height > 0:
                x, y = area.x(), y + line_height + v_gap
                line_height = 0
            if apply:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x += hint.width() + h_gap
            line_height = max(line_height, hint.height())
        return y + line_height - rect.y() + m.bottom()


def wrapping_row(widgets) -> QWidget:
    """Widgets in a row that wraps onto further lines in narrow windows."""
    row = QWidget()
    layout = FlowLayout(row)
    for w in widgets:
        layout.addWidget(w)
    policy = QSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
    policy.setHeightForWidth(True)
    row.setSizePolicy(policy)
    return row
