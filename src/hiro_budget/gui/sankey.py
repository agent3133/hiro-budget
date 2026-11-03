"""A Sankey diagram drawn with QPainter (Qt Charts has none): nodes in columns, ribbons
as wide as the amount between nodes of neighbouring columns.

Every node keeps at least one text line of height, so the labels never overlap; the
widget asks for the height that needs (put it in a scroll area). Labels stand left of
their bar (in the middle columns over the ribbons that come in, on a veil of the card
colour), those of the last column right of it.
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontMetrics,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPalette,
    QPen,
)
from PySide6.QtWidgets import QMenu, QSizePolicy, QToolTip, QWidget

from hiro_budget.gui import theme
from hiro_budget.gui.common import is_dark


@dataclass
class SankeyNode:
    column: int
    label: str
    value_text: str  # shown after the label, in the muted colour
    value: float
    color: QColor
    tooltip: str = ""


@dataclass
class SankeyLink:
    source: int  # node index, column n
    target: int  # node index, column n + 1
    value: float
    tooltip: str = ""


NODE_WIDTH = 12
GAP = 4  # between the slots of a column
PAD = 12  # inside the card
LABEL_PAD = 6  # between a bar and its label
TITLE_GAP = 10
MAX_OUTER_LABEL = 0.28  # share of the width for the labels of the outer columns
RIBBON_ALPHA, RIBBON_MAX_ALPHA = 0.38, 0.75  # more for colours close to the card's
RIBBON_HOVER, RIBBON_DIM = 0.3, 0.35  # added when hovered / factor for the others


@dataclass
class _Box:
    rect: QRectF  # the bar
    slot: QRectF  # the bar's line of the column (at least a text line high)


class SankeyChart(QWidget):
    #: a node was double-clicked (its index)
    node_activated = Signal(int)

    def __init__(self, title: str = "", parent=None) -> None:
        super().__init__(parent)
        self.title = title
        self.nodes: list[SankeyNode] = []
        self.links: list[SankeyLink] = []
        self.empty_text = ""
        # a node's breakdown also from the keyboard: Shift+F10 / menu key (#70)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self.drill_menu)
        self._boxes: list[_Box] = []
        self._paths: list[QPainterPath] = []
        self._hover: tuple[str, int] | None = None  # ("node" | "link", index)
        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def set_data(self, nodes: list[SankeyNode], links: list[SankeyLink], empty_text: str = ""):
        self.nodes, self.links, self.empty_text = nodes, links, empty_text
        self._hover = None
        self._boxes, self._paths = [], []
        self.updateGeometry()  # the minimum height depends on the nodes
        self.update()

    # -- geometry ---------------------------------------------------------------------

    def _columns(self) -> list[list[int]]:
        count = max((n.column for n in self.nodes), default=-1) + 1
        columns: list[list[int]] = [[] for _ in range(count)]
        for i, n in enumerate(self.nodes):
            columns[n.column].append(i)
        return columns

    def _line(self) -> int:
        return QFontMetrics(self.font()).height()

    def _title_height(self) -> int:
        return QFontMetrics(self._title_font()).height() + TITLE_GAP if self.title else 0

    def minimumSizeHint(self) -> QSize:
        most = max((len(c) for c in self._columns()), default=0)
        slots = most * self._line() + max(most - 1, 0) * GAP
        return QSize(400, 2 * PAD + self._title_height() + max(slots + 60, 160))

    def sizeHint(self) -> QSize:
        return self.minimumSizeHint().expandedTo(QSize(800, 500))

    def _layout(self) -> None:
        self._boxes = [_Box(QRectF(), QRectF()) for _ in self.nodes]
        self._paths = []
        columns = self._columns()
        if not columns:
            return
        fm = QFontMetrics(self.font())
        line = self._line()
        top = PAD + self._title_height()
        height = self.height() - top - PAD
        width = self.width() - 2 * PAD
        cap = width * MAX_OUTER_LABEL
        left = min(cap, max((self._label_width(fm, i) for i in columns[0]), default=0))
        right = (
            min(cap, max((self._label_width(fm, i) for i in columns[-1]), default=0))
            if len(columns) > 1
            else 0
        )
        x_first = PAD + left + LABEL_PAD
        x_last = self.width() - PAD - right - LABEL_PAD - NODE_WIDTH
        step = (x_last - x_first) / (len(columns) - 1) if len(columns) > 1 else 0

        def needed(scale: float, column: list[int]) -> float:
            slots = sum(max(self.nodes[i].value * scale, line) for i in column)
            return slots + GAP * (len(column) - 1)

        # the largest scale at which every column fits (slots never below a text line)
        biggest = max(sum(self.nodes[i].value for i in c) for c in columns) or 1
        lo, hi = 0.0, height / biggest
        for _ in range(40):
            mid = (lo + hi) / 2
            if all(needed(mid, c) <= height for c in columns):
                lo = mid
            else:
                hi = mid
        scale = lo
        for col, column in enumerate(columns):
            x = x_first + col * step
            y = top + max(0.0, (height - needed(scale, column)) / 2)
            for i in column:
                bar = self.nodes[i].value * scale
                slot = max(bar, line)
                self._boxes[i] = _Box(
                    QRectF(x, y + (slot - bar) / 2, NODE_WIDTH, bar),
                    QRectF(x, y, NODE_WIDTH, slot),
                )
                y += slot + GAP

        # ribbons: stacked on each bar in the order of the nodes at their other end
        def stack(end: str, other: str) -> list[float]:
            """Offset of each link on its ``end`` node, ordered by its ``other`` node."""
            offsets, used = [0.0] * len(self.links), [0.0] * len(self.nodes)
            for k in sorted(
                range(len(self.links)),
                key=lambda k: self._boxes[getattr(self.links[k], other)].rect.top(),
            ):
                node = getattr(self.links[k], end)
                offsets[k] = used[node]
                used[node] += self.links[k].value * scale
            return offsets

        out_offsets, in_offsets = stack("source", "target"), stack("target", "source")
        for k, link in enumerate(self.links):
            src, dst = self._boxes[link.source].rect, self._boxes[link.target].rect
            thick = link.value * scale
            y0, y1 = src.top() + out_offsets[k], dst.top() + in_offsets[k]
            x0, x1 = src.right(), dst.left()
            xm = (x0 + x1) / 2
            path = QPainterPath(QPointF(x0, y0))
            path.cubicTo(QPointF(xm, y0), QPointF(xm, y1), QPointF(x1, y1))
            path.lineTo(x1, y1 + thick)
            path.cubicTo(QPointF(xm, y1 + thick), QPointF(xm, y0 + thick), QPointF(x0, y0 + thick))
            path.closeSubpath()
            self._paths.append(path)

    def _label_width(self, fm: QFontMetrics, i: int) -> int:
        n = self.nodes[i]
        return fm.horizontalAdvance(n.label) + fm.horizontalAdvance("  " + n.value_text)

    def resizeEvent(self, event) -> None:
        self._boxes = []  # laid out again on the next paint
        super().resizeEvent(event)

    # -- painting ---------------------------------------------------------------------

    def _title_font(self) -> QFont:
        font = QFont(self.font())
        font.setPointSizeF(font.pointSizeF() * 1.1)
        font.setBold(True)
        return font

    def _colors(self) -> tuple[QColor, QColor, QColor, QColor, QColor]:
        """Card, border, text, muted text, title."""
        t = theme.tokens()
        if t is not None:
            return (
                QColor(t["input"]),
                QColor(t["border"]),
                QColor(t["text"]),
                QColor(t["muted"]),
                QColor(t["heading"]),
            )
        pal = self.palette()
        text = pal.color(QPalette.ColorRole.WindowText)
        border = QColor(255, 255, 255, 20) if is_dark() else QColor(0, 0, 0, 14)
        return (
            pal.color(QPalette.ColorRole.Base),
            border,
            text,
            pal.color(QPalette.ColorRole.PlaceholderText),
            text,
        )

    def paintEvent(self, event) -> None:
        if len(self._boxes) != len(self.nodes):
            self._layout()
        card, border, text, muted, heading = self._colors()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(QPen(border, 1))
        p.setBrush(card)
        p.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 4, 4)
        if self.title:
            p.setFont(self._title_font())
            p.setPen(heading)
            p.drawText(
                QRectF(PAD, PAD, self.width() - 2 * PAD, self._title_height()),
                Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
                self.title,
            )
            p.setFont(self.font())
        if not self.nodes:
            p.setPen(muted)
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self.empty_text)
            return

        active = self._active_links()
        p.setPen(Qt.PenStyle.NoPen)
        for k, path in enumerate(self._paths):
            link = self.links[k]
            box0, box1 = self._boxes[link.source].rect, self._boxes[link.target].rect
            gradient = QLinearGradient(box0.right(), 0, box1.left(), 0)
            for at, node in ((0, link.source), (1, link.target)):
                color = self.nodes[node].color
                alpha = _ribbon_alpha(color, card)
                if active is not None:
                    alpha = min(1.0, alpha + RIBBON_HOVER) if k in active else alpha * RIBBON_DIM
                gradient.setColorAt(at, _alpha(color, alpha))
            p.setBrush(QBrush(gradient))
            p.drawPath(path)

        for i, box in enumerate(self._boxes):
            p.setBrush(self.nodes[i].color)
            p.drawRoundedRect(box.rect.adjusted(0, 0, 0, max(0.0, 1 - box.rect.height())), 2, 2)

        fm = QFontMetrics(self.font())
        last = max(n.column for n in self.nodes)
        label_space = self._label_space()
        for i, n in enumerate(self.nodes):
            slot = self._boxes[i].slot
            to_left = _faces_left(n.column, last)
            room = label_space[n.column]
            value = "  " + n.value_text
            name = fm.elidedText(
                n.label, Qt.TextElideMode.ElideRight, max(0, room - fm.horizontalAdvance(value))
            )
            w = fm.horizontalAdvance(name) + fm.horizontalAdvance(value)
            x = slot.left() - LABEL_PAD - w if to_left else slot.right() + LABEL_PAD
            y = slot.center().y() - fm.height() / 2
            if 0 < n.column < last:  # over the ribbons: on a veil of the card colour
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(_alpha(card, 0.8))
                p.drawRoundedRect(QRectF(x - 3, y, w + 6, fm.height()), 3, 3)
            p.setPen(text)
            p.drawText(QPointF(x, y + fm.ascent()), name)
            p.setPen(muted)
            p.drawText(QPointF(x + fm.horizontalAdvance(name), y + fm.ascent()), value)

    def _label_space(self) -> dict[int, float]:
        """Width for the labels of each column: the way to the previous column, the
        margins for the outer ones."""
        last = max(n.column for n in self.nodes)
        xs = {n.column: self._boxes[i].rect.left() for i, n in enumerate(self.nodes)}
        space = {}
        for col, x in xs.items():
            if col == 0:
                space[col] = x - PAD - LABEL_PAD
            elif col == last:
                space[col] = self.width() - PAD - x - NODE_WIDTH - LABEL_PAD
            else:
                space[col] = x - xs.get(col - 1, x) - NODE_WIDTH - 2 * LABEL_PAD - 8
        return space

    def _active_links(self) -> set[int] | None:
        if self._hover is None:
            return None
        kind, index = self._hover
        if kind == "link":
            return {index}
        return {k for k, link in enumerate(self.links) if index in (link.source, link.target)}

    # -- interaction ------------------------------------------------------------------

    def _hit(self, pos: QPointF) -> tuple[str, int] | None:
        for i, box in enumerate(self._boxes):
            if box.slot.adjusted(-3, 0, 3, 0).contains(pos):
                return ("node", i)
        for k in range(len(self._paths) - 1, -1, -1):
            if self._paths[k].contains(pos):
                return ("link", k)
        return None

    def mouseMoveEvent(self, event) -> None:
        hit = self._hit(event.position())
        if hit != self._hover:
            self._hover = hit
            self.update()
        if hit is None:
            QToolTip.hideText()
        else:
            kind, index = hit
            tip = self.nodes[index].tooltip if kind == "node" else self.links[index].tooltip
            QToolTip.showText(event.globalPosition().toPoint(), tip, self)
        super().mouseMoveEvent(event)

    def leaveEvent(self, event) -> None:
        if self._hover is not None:
            self._hover = None
            self.update()
        super().leaveEvent(event)

    def drill_menu(self, pos) -> None:
        """The nodes as a menu, column by column; an entry opens what a double-click does."""
        if not self.nodes:
            return
        menu = QMenu(self)
        column = None
        for i in sorted(range(len(self.nodes)), key=lambda i: self.nodes[i].column):
            node = self.nodes[i]
            if column is not None and node.column != column:
                menu.addSeparator()
            column = node.column
            menu.addAction(
                f"{node.label}  {node.value_text}", lambda i=i: self.node_activated.emit(i)
            )
        menu.exec(self.mapToGlobal(pos))

    def mouseDoubleClickEvent(self, event) -> None:
        hit = self._hit(event.position())
        if hit is not None and hit[0] == "node":
            self.node_activated.emit(hit[1])
        super().mouseDoubleClickEvent(event)


def _faces_left(column: int, last: int) -> bool:
    """Whether a column's labels stand left of its bars."""
    return column < last


def _ribbon_alpha(color: QColor, card: QColor) -> float:
    """Opacity of a ribbon: pale colours (e.g. pastel on a light card) get more, so that
    every ribbon stands out about as much from the card."""
    contrast = max(abs(color.lightness() - card.lightness()), 1)
    return min(RIBBON_MAX_ALPHA, max(RIBBON_ALPHA, RIBBON_ALPHA * 100 / contrast))


def _alpha(color: QColor, alpha: float) -> QColor:
    c = QColor(color)
    c.setAlphaF(alpha)
    return c
