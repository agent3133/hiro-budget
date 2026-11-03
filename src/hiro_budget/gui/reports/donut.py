"""The fixed costs donut's slices, colours and legend sizes (also the Overview's)."""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import Qt
from PySide6.QtGui import (
    QBrush,
    QColor,
    QIcon,
    QPainter,
    QPalette,
    QPixmap,
)

from hiro_budget.services import (
    recurring,
)


@dataclass
class Segment:
    """A slice of the fixed costs donut."""

    name: str
    cents: int  # per month
    items: list[recurring.Recurring]  # the payments in it (none for the variable costs)
    lines: list[str]  # tooltip lines below the heading
    brush: QBrush | None = None  # None: the chart theme's colour


DONUT_SIZE = 0.9  # of the plot area, as the hole
DONUT_HOLE = 0.52
DONUT_MAX_FIXED = 7  # fixed cost slices; with the variable costs at most 8
DONUT_LABEL_SHARE = 5  # % from which a slice shows its share on the ring
LEGEND_DOT = 10
LEGEND_ROW_GAP = 6
LEGEND_SIDE = 16  # left and right of the legend
SHADE_STEPS = (0, 14, -14, 26, -26, 36)  # lightness offsets (HSL, 0-255) within a group


def dot_icon(color: QColor, size: int = 10) -> QIcon:
    """A filled circle, e.g. a slice's colour in a table cell."""
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(color)
    painter.drawEllipse(0, 0, size, size)
    painter.end()
    return QIcon(pixmap)


def donut_ring(chart, palette: QPalette) -> QColor:
    """The colour of the gap between donut slices: the card's (a theme without one draws
    a gradient, whose colour would be black)."""
    brush = chart.backgroundBrush()
    if brush.style() == Qt.BrushStyle.SolidPattern:
        return brush.color()
    return palette.color(QPalette.ColorRole.Base)


def shade(color: QColor, index: int) -> QColor:
    """The ``index``-th shade of a group colour (lighter and darker in turn; the first is
    the colour itself, as the budget's group chip)."""
    step = SHADE_STEPS[index % len(SHADE_STEPS)]
    if not step:
        return QColor(color)
    h, s, lightness, a = color.getHsl()
    return QColor.fromHsl(h, s, max(40, min(225, lightness + step)), a)


# --- mortgage --------------------------------------------------------------------------
