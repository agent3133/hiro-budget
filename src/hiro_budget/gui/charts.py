"""Chart helpers shared by the report pages (QtCharts, light/dark aware)."""

from __future__ import annotations

import math

from PySide6.QtCharts import (
    QBarCategoryAxis,
    QBarSeries,
    QBarSet,
    QChart,
    QChartView,
    QLineSeries,
    QValueAxis,
)
from PySide6.QtCore import QCoreApplication, QMargins, QPointF, Qt
from PySide6.QtGui import QColor, QCursor, QPainter, QPen
from PySide6.QtWidgets import QFrame, QHeaderView, QTableWidget, QTableWidgetItem, QToolTip

from hiro_budget.gui import theme
from hiro_budget.gui.common import amount_color, flat_table, is_dark, money

INCOME_COLOR = "#2e9e5b"
EXPENSE_COLOR = "#d9534f"


def income_color() -> QColor:
    t = theme.tokens()
    return QColor(t["income"] if t else INCOME_COLOR)


def expense_color() -> QColor:
    t = theme.tokens()
    return QColor(t["expense"] if t else EXPENSE_COLOR)


def new_chart(title: str) -> QChart:
    chart = QChart()
    chart.setTitle(title)
    chart.setTheme(
        QChart.ChartTheme.ChartThemeDark if is_dark() else QChart.ChartTheme.ChartThemeLight
    )
    chart.setAnimationOptions(QChart.AnimationOption.NoAnimation)
    chart.setMargins(QMargins(8, 4, 8, 4))  # room inside the card
    # The card (chart background) looks like the frame of the tables next to it: same
    # inset (~1.5 px instead of the ~10 px of QtCharts), same corner rounding and a thin
    # border in the Windows 11 stroke colour.
    chart.layout().setContentsMargins(1.5, 1.5, 1.5, 1.5)
    chart.setBackgroundRoundness(4)
    stroke = QColor(255, 255, 255, 20) if is_dark() else QColor(0, 0, 0, 14)
    t = theme.tokens()
    if t is not None:  # a card of the custom theme
        stroke = QColor(t["border"])
        chart.setBackgroundBrush(QColor(t["input"]))
        chart.setTitleBrush(QColor(t["heading"]))
    chart.setBackgroundPen(QPen(stroke, 1))
    chart.legend().setAlignment(Qt.AlignmentFlag.AlignBottom)
    return chart


def chart_view(chart: QChart) -> QChartView:
    v = QChartView(chart)
    v.setRenderHint(QPainter.RenderHint.Antialiasing)
    v.setFrameShape(QFrame.Shape.NoFrame)
    v.setContentsMargins(0, 0, 0, 0)
    v.setMinimumHeight(200)  # two charts above each other fit a laptop screen
    return v


def set_chart(view: QChartView, chart: QChart) -> None:
    """Replace a view's chart; the view releases (but does not delete) the old one."""
    old = view.chart()
    view.setChart(chart)
    if old is not None:
        old.deleteLater()


def add_bar_tooltips(series: QBarSeries, labels: list[str]) -> None:
    """Show 'set · month: amount' while hovering a bar."""

    def hovered(status: bool, index: int, bar_set: QBarSet) -> None:
        if status:
            text = f"{bar_set.label()} · {labels[index]}: {money(round(bar_set.at(index) * 100))}"
            QToolTip.showText(QCursor.pos(), text)
        else:
            QToolTip.hideText()

    series.hovered.connect(hovered)


def nice_max(value: float) -> float:
    """Round an axis maximum up to a readable number."""
    if value <= 0:
        return 100
    target = value * 1.05  # a little headroom above the largest bar
    magnitude = 10 ** (len(str(int(target))) - 1)
    for step in (1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10):
        if step * magnitude >= target:
            return step * magnitude
    return 10 * magnitude


def one_off_name(name: str, one_off: bool) -> str:
    """A category name with the one-off marker."""
    if not one_off:
        return name
    return QCoreApplication.translate("report_pages", "%1 (one-off)").replace("%1", name)


def month_label(month: str) -> str:
    y, m = month.split("-")
    return f"{m}/{y[2:]}"


def nice_min(value: float) -> float:
    return -nice_max(-value) if value < 0 else 0


# --- building blocks for the report pages ---------------------------------------------


def month_axis(months: list[str]) -> QBarCategoryAxis:
    axis = QBarCategoryAxis()
    axis.append([month_label(m) for m in months])
    return axis


def money_axis(values: list[float], title: str = "EUR", tight: bool = False) -> QValueAxis:
    """Value axis including 0, or (``tight``) just around the values to show changes."""
    axis = QValueAxis()
    if tight and values:
        low, high = min(values), max(values)
        step = nice_max((high - low) / 4 or abs(high) / 10 or 1)
        axis.setRange(math.floor(low / step) * step, math.ceil(high / step) * step)
        axis.applyNiceNumbers()  # round tick labels
    else:
        axis.setRange(nice_min(min(values, default=0)), nice_max(max(values, default=0)))
    axis.setLabelFormat("%.0f")
    axis.setTitleText(title)
    return axis


def line_series(name: str, values: list[float | None], color: QColor) -> QLineSeries:
    """One point per month (x = month index), drawn with markers; ``None`` is left out."""
    series = QLineSeries()
    series.setName(name)
    series.setColor(color)
    series.setPointsVisible(True)
    for i, v in enumerate(values):
        if v is not None:
            series.append(i, v)
    pen = series.pen()
    pen.setWidth(2)
    series.setPen(pen)
    return series


def add_line_tooltips(series: QLineSeries, labels: list[str], unit: str = "€") -> None:
    """Show 'series · month: value' while hovering a point."""

    def hovered(point: QPointF, state: bool) -> None:
        index = round(point.x())
        if state and 0 <= index < len(labels):
            value = point.y()
            text = money(round(value * 100)) if unit == "€" else f"{value:.1f} {unit}"
            QToolTip.showText(QCursor.pos(), f"{series.name()} · {labels[index]}: {text}")
        else:
            QToolTip.hideText()

    series.hovered.connect(hovered)


def attach(chart: QChart, series, x_axis, y_axis) -> None:
    sides = ((x_axis, Qt.AlignmentFlag.AlignBottom), (y_axis, Qt.AlignmentFlag.AlignLeft))
    for axis, side in sides:
        if axis not in chart.axes():
            chart.addAxis(axis, side)
    if series not in chart.series():
        chart.addSeries(series)
    series.attachAxis(x_axis)
    series.attachAxis(y_axis)


def report_table(headers: list[str], numeric: set[int]) -> QTableWidget:
    """Read-only table; the first column stretches, ``numeric`` columns align right."""
    table = QTableWidget(0, len(headers))
    flat_table(table)
    table.setHorizontalHeaderLabels(headers)
    table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
    table.verticalHeader().setVisible(False)
    table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
    table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
    table.setProperty("numeric", sorted(numeric))
    return table


def fill_table(
    table: QTableWidget,
    rows: list[list],
    bold_last: bool = False,
    signed: frozenset[int] = frozenset(),
    costs: frozenset[int] = frozenset(),
) -> None:
    """Fill rows; an ``int`` cell is an amount in cents, anything else is shown as text.
    Amounts in ``signed`` columns are coloured green / red by their sign; in ``costs``
    columns the other way round (more expenses: red)."""
    numeric = set(table.property("numeric") or [])
    table.setRowCount(len(rows))
    for r, row in enumerate(rows):
        for c, value in enumerate(row):
            item = QTableWidgetItem(money(value) if isinstance(value, int) else str(value))
            if c in numeric:
                item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            if c in signed and isinstance(value, int) and value:
                item.setForeground(amount_color(value))
            if c in costs and isinstance(value, int) and value:
                item.setForeground(amount_color(-value))
            if bold_last and r == len(rows) - 1:
                font = item.font()
                font.setBold(True)
                item.setFont(font)
            table.setItem(r, c, item)
    table.resizeColumnsToContents()
    table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
