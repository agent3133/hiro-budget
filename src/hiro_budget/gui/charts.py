"""Chart helpers shared by the report pages (QtCharts, light/dark aware)."""

from __future__ import annotations

import math
from datetime import date

from PySide6.QtCharts import (
    QAbstractBarSeries,
    QBarCategoryAxis,
    QBarSeries,
    QBarSet,
    QCategoryAxis,
    QChart,
    QChartView,
    QLineSeries,
    QPieSeries,
    QValueAxis,
)
from PySide6.QtCore import SIGNAL, QCoreApplication, QLocale, QMargins, QPointF, Qt
from PySide6.QtGui import QBrush, QColor, QCursor, QFont, QFontMetrics, QPainter, QPalette, QPen
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QGraphicsEllipseItem,
    QGraphicsSimpleTextItem,
    QHeaderView,
    QMenu,
    QTableWidget,
    QTableWidgetItem,
    QToolTip,
)

from hiro_budget.gui import theme
from hiro_budget.gui.common import amount_color, flat_table, is_dark, money


def income_color() -> QColor:
    return QColor(theme.color("income"))


def expense_color() -> QColor:
    return QColor(theme.color("expense"))


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
    # the breakdowns of a double-click also from the keyboard: Tab / F6 to the chart,
    # then Shift+F10 or the menu key (#70); a right-click opens the same menu
    v.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
    v.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
    v.customContextMenuRequested.connect(lambda pos, v=v: drill_menu(v, pos))
    return v


def set_drill_labels(series: QAbstractBarSeries, labels: list[str]) -> None:
    """Names of the bars in the breakdown menu, when the axis does not name each one."""
    series.setProperty("drill_labels", list(labels))


def _bar_labels(series: QAbstractBarSeries) -> list[str]:
    labels = series.property("drill_labels")
    if labels:
        return list(labels)
    for axis in series.attachedAxes():
        if isinstance(axis, QBarCategoryAxis):
            return [strip_markup(c) for c in axis.categories()]
    return []


def drill_entries(chart: QChart | None) -> list[tuple[str, list[tuple[str, object]]]]:
    """What a double-click (or a click) on the chart opens, as menu entries:
    [(bar set or series name, [(bar or slice name, action)])]."""
    groups: list[tuple[str, list[tuple[str, object]]]] = []
    if chart is None:
        return groups
    for series in chart.series():
        if isinstance(series, QAbstractBarSeries):
            labels = _bar_labels(series)
            for bar_set in series.barSets():
                if bar_set.receivers(SIGNAL("doubleClicked(int)")) == 0:
                    continue
                items = [
                    (
                        labels[i] if i < len(labels) else str(i + 1),
                        lambda b=bar_set, i=i: b.doubleClicked.emit(i),
                    )
                    for i in range(bar_set.count())
                    if bar_set.at(i)
                ]
                if items:
                    groups.append((bar_set.label(), items))
        elif isinstance(series, QPieSeries):
            items = []
            for piece in series.slices():
                name = piece.property("drill_label") or piece.label()
                if piece.receivers(SIGNAL("doubleClicked()")):
                    items.append((name, lambda p=piece: p.doubleClicked.emit()))
                elif piece.receivers(SIGNAL("clicked()")):
                    items.append((name, lambda p=piece: p.clicked.emit()))
            if items:
                groups.append((series.name(), items))
    return groups


def drill_menu(view: QChartView, pos) -> None:
    """The chart's breakdowns as a menu (one submenu per bar set if there are more)."""
    groups = drill_entries(view.chart())
    if not groups:
        return
    menu = QMenu(view)
    menu.setTitle(QCoreApplication.translate("charts", "Breakdown"))
    for title, items in groups:
        target = menu.addMenu(title or "…") if len(groups) > 1 else menu
        for label, action in items:
            target.addAction(label, action)
    menu.exec(view.mapToGlobal(pos))


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
    set_drill_labels(series, labels)


def one_off_name(name: str, one_off: bool) -> str:
    """A category name with the one-off marker."""
    if not one_off:
        return name
    return QCoreApplication.translate("report_pages", "%1 (one-off)").replace("%1", name)


def month_label(month: str) -> str:
    y, m = month.split("-")
    return f"{m}/{y[2:]}"


def month_labels(months: list[str], partial: tuple[str, date] | None = None) -> list[str]:
    """ "10/26" per month; a month with data for only part of it says up to when
    ("10/26" over "until 22.10." under a bar)."""
    labels = [month_label(m) for m in months]
    if partial is not None and partial[0] in months:
        i = months.index(partial[0])
        labels[i] = (
            QCoreApplication.translate("charts", "%1<br><small>until %2</small>")
            .replace("%1", labels[i])
            .replace("%2", partial[1].strftime("%d.%m."))
        )
    return labels


def strip_markup(label: str) -> str:
    """An axis label as plain text, on one line (tooltips, tables)."""
    return label.replace("<br>", " ").replace("<small>", "").replace("</small>", "")


def card_color() -> QColor:
    """The colour of the chart card (behind the bars) in the theme in use."""
    t = theme.tokens()
    if t is not None:
        return QColor(t["input"])
    # QtCharts' own backgrounds: white, or a gradient #2e303a → #121218 (its middle)
    return QColor("#202129") if is_dark() else QColor("#ffffff")


def new_bar_set(label: str, color: QColor) -> QBarSet:
    """A bar set in ``color`` with an outline in the card colour: a thin gap between
    neighbouring bars in both themes (QtCharts draws a white outline, bright on a dark
    card)."""
    bars = QBarSet(label)
    bars.setColor(color)
    bars.setPen(QPen(card_color(), 1))
    return bars


def blend(color: QColor, other: QColor, share: float) -> QColor:
    """``color`` mixed with ``share`` of ``other``, opaque."""
    return QColor(
        round(color.red() * (1 - share) + other.red() * share),
        round(color.green() * (1 - share) + other.green() * share),
        round(color.blue() * (1 - share) + other.blue() * share),
    )


def faded(color: QColor) -> QColor:
    """``color`` washed out, opaque: towards the card on a light card; on a dark card
    towards the muted text grey, since towards the card it would only turn darker (amber
    became brown and looked like another series, #53)."""
    if not is_dark():
        return blend(color, card_color(), 0.6)
    t = theme.tokens()
    grey = QColor(t["muted"]) if t is not None else QColor("#a0a0a8")
    return blend(color, grey, 0.6)


def mark_partial(bar_sets: list[QBarSet], months: list[str], partial) -> None:
    """Draw the bars of an incomplete month faded (the selected-bar colour of each set,
    nothing else selects bars)."""
    if partial is None or partial[0] not in months:
        return
    index = months.index(partial[0])
    for bars in bar_sets:
        bars.setSelectedColor(faded(QColor(bars.color())))
        bars.selectBar(index)


# --- building blocks for the report pages ---------------------------------------------


def month_axis(months: list[str], partial: tuple[str, date] | None = None) -> QBarCategoryAxis:
    axis = QBarCategoryAxis()
    axis.append(month_labels(months, partial))
    return axis


def sparse_month_axis(months: list[str], max_labels: int = 12) -> QCategoryAxis:
    """An x-axis for a line over many months (x = month index): a label only every 1, 3,
    6, 12, 24, 60 or 120 months (Januaries from a year on), at most ``max_labels``."""
    axis = QCategoryAxis()
    axis.setLabelsPosition(QCategoryAxis.AxisLabelsPosition.AxisLabelsPositionOnValue)
    axis.setRange(-0.5, len(months) - 0.5)
    step = next((s for s in (1, 3, 6, 12, 24, 60, 120) if len(months) / s <= max_labels), 240)
    for i, m in enumerate(months):
        year, month = int(m[:4]), int(m[5:])
        if step < 12 and (month - 1) % step == 0:
            axis.append(month_label(m), i)
        elif step >= 12 and month == 1 and year % (step // 12) == 0:
            axis.append(str(year), i)
    return axis


def nice_step(span: float, intervals: int = 5) -> float:
    """A readable tick step (1, 2, 2.5 or 5 × 10ⁿ) giving at most ``intervals`` intervals."""
    if span <= 0:
        return 1
    raw = span / intervals
    magnitude = 10 ** math.floor(math.log10(raw))
    for m in (1, 2, 2.5, 5, 10):
        if m * magnitude >= raw * (1 - 1e-9):
            return m * magnitude
    return 10 * magnitude


def axis_ticks(low: float, high: float, intervals: int = 5) -> list[float]:
    """Tick values from a multiple of the step at or below ``low`` to one at or above
    ``high``; 0 is one of them whenever the range spans it."""
    if high <= low:  # a single value: a band around it
        pad = abs(low) / 10 or 100
        low, high = low - pad, high + pad
    step = nice_step(high - low, intervals)
    first, last = math.floor(low / step + 1e-9), math.ceil(high / step - 1e-9)
    return [k * step for k in range(first, last + 1)]


def axis_number(value: float) -> str:
    """An axis label in the locale's format ("−80.000", "2,5")."""
    if abs(value - round(value)) < 1e-9:
        return QLocale().toString(round(value))
    return QLocale().toString(value, "f", 1 if abs(value * 10 - round(value * 10)) < 1e-9 else 2)


def money_axis(
    values: list[float], title: str = "EUR", tight: bool = False, intervals: int = 5
) -> QValueAxis:
    """Value axis including 0, or (``tight``) just around the values to show changes;
    at most ``intervals`` steps (fewer for a small chart, so the labels fit).

    The step comes first (1, 2, 2.5, 5 × 10ⁿ), both ends are multiples of it, so 0 is on a
    grid line; labels have thousands separators (a QCategoryAxis with a label per tick)."""
    low, high = min(values, default=0), max(values, default=0)
    if not tight:
        low, high = min(low, 0), max(high, 0)
        # a little headroom above the largest bar (below the lowest)
        high, low = high * 1.05 if high > 0 else high, low * 1.05 if low < 0 else low
        if high == low:  # nothing to show
            high = 100
    ticks = axis_ticks(low, high, intervals)
    axis = QCategoryAxis()
    axis.setLabelsPosition(QCategoryAxis.AxisLabelsPosition.AxisLabelsPositionOnValue)
    axis.setRange(ticks[0], ticks[-1])
    axis.setStartValue(ticks[0])
    for tick in ticks:
        axis.append(axis_number(tick), tick)
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


def money_bar_labels(chart: QChart, series, values: list[float]) -> list:
    """Amounts in the money format ("2.797,81 €") after the ends of a horizontal bar
    series' bars; QtCharts' own labels (``@value``) can't format money. The labels follow
    the plot area when the chart is resized."""
    color = QApplication.palette().color(QPalette.ColorRole.WindowText)
    items = []
    for value in values:
        item = QGraphicsSimpleTextItem(money(round(value * 100)), chart)
        item.setBrush(color)
        item.setZValue(10)
        items.append(item)

    def place() -> None:
        for i, (item, value) in enumerate(zip(items, values, strict=True)):
            end = chart.mapToPosition(QPointF(value, i), series)
            box = item.boundingRect()
            item.setPos(end.x() + 4, end.y() - box.height() / 2)

    chart.plotAreaChanged.connect(place)
    place()
    return items


def style_axis(axis) -> None:
    """Grid and axis lines in the custom theme's line colours (Qt's dark chart theme
    draws them bright)."""
    t = theme.tokens()
    if t is not None:
        axis.setGridLineColor(QColor(t["line"]))
        axis.setLinePenColor(QColor(t["border"]))


def add_bottom_money_axis(chart: QChart, axis: QCategoryAxis) -> None:
    """A money axis along the bottom (horizontal bars), with room on the right for its
    last label, which is centred on the plot area's edge."""
    chart.addAxis(axis, Qt.AlignmentFlag.AlignBottom)
    style_axis(axis)
    labels = axis.categoriesLabels()
    if labels:
        half = QFontMetrics(axis.labelsFont()).horizontalAdvance(labels[-1]) // 2
        margins = chart.margins()
        margins.setRight(max(margins.right(), half + 4))
        chart.setMargins(margins)


def attach(chart: QChart, series, x_axis, y_axis) -> None:
    sides = ((x_axis, Qt.AlignmentFlag.AlignBottom), (y_axis, Qt.AlignmentFlag.AlignLeft))
    for axis, side in sides:
        if axis not in chart.axes():
            chart.addAxis(axis, side)
            style_axis(axis)
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


def text_label(
    chart: QChart, base: QFont, text: str, size: float = 1.0, bold: bool = False
) -> QGraphicsSimpleTextItem:
    """A text drawn into a chart (a donut's total, a legend line) in the title's colour,
    ``size`` times the ``base`` font."""
    item = QGraphicsSimpleTextItem(text, chart)
    font = QFont(base)
    font.setPointSizeF(font.pointSizeF() * size)
    font.setBold(bold)
    item.setFont(font)
    item.setBrush(chart.titleBrush())
    return item


def legend_dot(chart: QChart, brush: QBrush, size: float) -> QGraphicsEllipseItem:
    """A legend's colour dot drawn into a chart; the outline shows a hatched brush too."""
    dot = QGraphicsEllipseItem(0, 0, size, size, chart)
    dot.setBrush(brush)
    dot.setPen(QPen(brush.color(), 1))
    return dot
