"""Fixed costs report: table and donut."""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import date

from PySide6.QtCharts import (
    QPieSeries,
    QPieSlice,
)
from PySide6.QtCore import QMargins, Qt
from PySide6.QtGui import (
    QBrush,
    QColor,
    QCursor,
    QFontMetrics,
    QPalette,
)
from PySide6.QtWidgets import (
    QGraphicsEllipseItem,
    QGraphicsSimpleTextItem,
    QHBoxLayout,
    QToolTip,
    QVBoxLayout,
)

from hiro_budget.app import App
from hiro_budget.gui.charts import (
    chart_view,
    fill_table,
    legend_dot,
    month_label,
    new_chart,
    report_table,
    set_chart,
    text_label,
)
from hiro_budget.gui.common import (
    fmt_date,
    fmt_number,
    group_colors,
    is_dark,
    money,
    warning_color,
)
from hiro_budget.gui.reports.base import PAGE_TOP_MARGIN, ReportPage, note_label
from hiro_budget.gui.reports.donut import (
    DONUT_HOLE,
    DONUT_LABEL_SHARE,
    DONUT_MAX_FIXED,
    DONUT_SIZE,
    LEGEND_DOT,
    LEGEND_ROW_GAP,
    LEGEND_SIDE,
    Segment,
    donut_ring,
    dot_icon,
    shade,
)
from hiro_budget.services import (
    recurring,
)


class RecurringReport(ReportPage):
    """Payments that recur at a regular rhythm, with their cost per month."""

    def __init__(self, app: App, parent=None) -> None:
        super().__init__(app, parent)
        self.data: recurring.RecurringCosts | None = None
        self.variable: recurring.VariableCosts | None = None
        self.view = chart_view(new_chart(self.tr("Fixed and variable costs per month")))
        self.table = report_table(
            [
                self.tr("Payee"),
                self.tr("Category"),
                self.tr("Rhythm"),
                self.tr("Amount"),
                self.tr("Per month"),
                self.tr("Last payment"),
                self.tr("Next due"),
            ],
            {3, 4},
        )
        self.note = note_label()
        self._center: list[QGraphicsSimpleTextItem] = []  # the total in the hole
        # per legend entry: dot, text, name, share
        self._legend: list[tuple[QGraphicsEllipseItem, QGraphicsSimpleTextItem, str, str]] = []
        self._dot_colors: dict[int, QColor] = {}  # slice colour by id() of a payment
        row = QHBoxLayout()
        row.addWidget(self.table, 3)
        row.addWidget(self.view, 2)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, PAGE_TOP_MARGIN, 0, 0)
        layout.addLayout(row, 1)
        layout.addWidget(self.note)

    def _rhythm(self, name: str) -> str:
        return {
            "monthly": self.tr("monthly"),
            "quarterly": self.tr("quarterly"),
            "half-yearly": self.tr("half-yearly"),
            "yearly": self.tr("yearly"),
        }.get(name, name)

    def refresh(
        self, start: date, end: date, account_id: int | None, one_offs: bool = True
    ) -> None:
        until = min(end, date.today())
        with self.app.session() as s:
            data = recurring.recurring_costs(s, until, account_id)
            variable = recurring.variable_costs(s, data, until, account_id, one_offs)
        self.data, self.variable = data, variable
        self._draw_chart(data, variable)
        rows = [
            [
                r.payee,
                r.category or "",
                self._rhythm(r.rhythm),
                r.amount_cents,
                r.monthly_cents,
                fmt_date(r.last),
                fmt_date(r.next_due),
            ]
            for r in data.items
        ]
        rows += [
            [
                r.payee,
                r.category or "",
                self.tr("%1, ended").replace("%1", self._rhythm(r.rhythm)),
                r.amount_cents,
                "",
                fmt_date(r.last),
                "",
            ]
            for r in data.ended
        ]
        rows.append(
            [
                self.tr("Total"),
                self.tr("%1 per year").replace("%1", money(data.monthly_cents * 12)),
                "",
                "",
                data.monthly_cents,
                "",
                "",
            ]
        )
        fill_table(self.table, rows, bold_last=True)
        for row, r in enumerate(data.items):  # the table is the donut's legend
            color = self._dot_colors.get(id(r))
            if color is not None:
                self.table.item(row, 1).setIcon(dot_icon(color))
        today = date.today()
        overdue = [row for row, r in enumerate(data.items) if r.next_due < today]
        for row in overdue:  # expected, not booked yet (#31)
            item = self.table.item(row, 6)
            item.setForeground(warning_color())
            font = item.font()
            font.setBold(True)  # the warning colour alone is close to the text colour
            item.setFont(font)
            item.setToolTip(
                self.tr(
                    "Overdue: expected on %1, not booked yet (paid late, ended, or a "
                    "statement is missing)"
                ).replace("%1", item.text())
            )
        grey = self.palette().color(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text)
        for row in range(len(data.items), len(data.items) + len(data.ended)):
            for col in range(self.table.columnCount()):
                self.table.item(row, col).setForeground(grey)
        self.note.setText(
            self.tr(
                "Payments that recurred over the %1 months up to %2 at a regular rhythm "
                "(monthly to yearly) with similar amounts, still running; ended ones in grey. "
                "Transfers to own budget accounts are not counted, loan instalments and "
                "savings plans are. Per month = amount spread over its rhythm."
            )
            .replace("%1", str(recurring.LOOKBACK_MONTHS))
            .replace("%2", fmt_date(until))
        )
        if overdue:
            self.note.setText(
                self.note.text()
                + " "
                + self.tr(
                    "%n payment(s) overdue (date in bold, warning colour): expected, but not "
                    "booked yet.",
                    "",
                    len(overdue),
                )
            )
        if variable is not None:
            self.note.setText(
                self.note.text()
                + " "
                + self.tr(
                    "Variable costs: all other expenses of %1–%2 (as in the other reports), "
                    "on average %3 per month."
                )
                .replace("%1", month_label(variable.first_month))
                .replace("%2", month_label(variable.last_month))
                .replace("%3", money(variable.monthly_cents))
            )

    def _draw_chart(
        self, data: recurring.RecurringCosts, variable: recurring.VariableCosts | None
    ) -> None:
        chart = new_chart(self.tr("Fixed and variable costs per month"))
        with self.app.session() as s:
            colors = group_colors(s)
        segments = self._segments(data, variable, colors)
        total = sum(seg.cents for seg in segments)
        series = QPieSeries()
        series.setHoleSize(DONUT_HOLE)
        series.setPieSize(DONUT_SIZE)
        series.setPieEndAngle(-360)  # from 12 o'clock counter-clockwise: the fixed costs on
        # the left, the variable costs on the right (as the legend reads)
        ring = donut_ring(chart, self.palette())  # gap between the slices
        ink = chart.titleBrush().color()
        pieces = []
        for seg in segments:
            share = seg.cents * 100 / total
            piece = series.append(f"{fmt_number(share, 0)} %", seg.cents / 100)
            if seg.brush is not None:
                piece.setBrush(seg.brush)
            piece.setBorderColor(ring)
            piece.setBorderWidth(2)
            piece.setLabelVisible(share >= DONUT_LABEL_SHARE)
            piece.setLabelColor(ink)
            piece.setLabelPosition(QPieSlice.LabelPosition.LabelInsideHorizontal)  # on the ring
            piece.setProperty("drill_label", seg.name)  # the breakdown menu (#70)
            heading = (
                self.tr("%1: %2 per month (%3 %)")
                .replace("%1", seg.name)
                .replace("%2", money(seg.cents))
                .replace("%3", fmt_number(share, 1))
            )
            tip = "\n".join([heading, *seg.lines])
            piece.hovered.connect(lambda on, p=piece, tip=tip: self._hover(p, on, tip))
            pieces.append(piece)
        if total:
            chart.addSeries(series)  # slices without a brush get the chart theme's colours
        chart.legend().hide()
        # read after addSeries: the colours the slices got
        self._dot_colors = {
            id(r): piece.color()
            for seg, piece in zip(segments, pieces, strict=True)
            for r in seg.items
        }
        self._center, self._legend = [], []
        if total:
            fixed = data.monthly_cents
            lines = [(money(fixed), 1.4, True), (self.tr("fixed per month"), 1.0, False)]
            if variable is not None:
                share = fmt_number(fixed * 100 / total, 0)
                lines.append((self.tr("%1 % of all costs").replace("%1", share), 1.0, False))
            self._center = [text_label(chart, self.font(), *line) for line in lines]
            self._legend = [
                (
                    legend_dot(chart, piece.brush(), LEGEND_DOT),
                    text_label(chart, self.font(), "", 1.0, False),
                    seg.name,
                    f"{fmt_number(seg.cents * 100 / total, 0)} %",
                )
                for seg, piece in zip(segments, pieces, strict=True)
            ]
            rows = math.ceil(len(segments) / 2)
            row_height = QFontMetrics(self.font()).height() + LEGEND_ROW_GAP
            chart.setMargins(QMargins(8, 4, 8, rows * row_height + 12))
        chart.plotAreaChanged.connect(lambda _area: self._place_overlays())
        set_chart(self.view, chart)
        self._place_overlays()

    def _segments(
        self,
        data: recurring.RecurringCosts,
        variable: recurring.VariableCosts | None,
        colors: dict[int, str],
    ) -> list[Segment]:
        """The donut's slices: each fixed payment, or per category when there are more
        than fit (the smallest then as "Other"), grouped by budget group (largest first) in
        shades of the group colour; the variable costs last."""
        uncategorized = self.tr("Uncategorized")
        if len(data.items) <= DONUT_MAX_FIXED:
            units = [
                Segment(r.payee, r.monthly_cents, [r], [r.category or uncategorized])
                for r in data.items
            ]
            groups = [r.group_id if r.category else None for r in data.items]
        else:
            per_category: dict[str, Segment] = {}
            group: dict[str, int | None] = {}
            for r in data.items:
                name = r.category or uncategorized
                seg = per_category.setdefault(name, Segment(name, 0, [], []))
                seg.cents += r.monthly_cents
                seg.items.append(r)
                seg.lines.append(r.payee)
                group.setdefault(name, r.group_id if r.category else None)
            units = list(per_category.values())
            groups = [group[seg.name] for seg in units]
        ranked = sorted(range(len(units)), key=lambda i: -units[i].cents)
        keep = ranked if len(units) <= DONUT_MAX_FIXED else ranked[: DONUT_MAX_FIXED - 1]
        per_group: dict[int | None, list[int]] = defaultdict(list)
        for i in keep:
            per_group[groups[i]].append(i)
        order = sorted(
            per_group, key=lambda g: (g is None, -sum(units[i].cents for i in per_group[g]))
        )
        neutral = self.palette().color(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text)
        out = []
        for g in order:
            base = QColor(colors[g]) if g in colors else (neutral if g is None else None)
            for n, i in enumerate(per_group[g]):
                seg = units[i]
                seg.brush = QBrush(shade(base, n)) if base is not None else None
                out.append(seg)
        rest = [units[i] for i in ranked[len(keep) :]]
        if rest:
            out.append(
                Segment(
                    self.tr("Other (%1)").replace("%1", str(len(rest))),
                    sum(seg.cents for seg in rest),
                    [r for seg in rest for r in seg.items],
                    [seg.name for seg in rest],
                    QBrush(neutral.lighter(130) if is_dark() else neutral.darker(115)),
                )
            )
        if variable is not None and variable.monthly_cents > 0:
            period = (
                self.tr("Average of the other expenses, %1–%2")
                .replace("%1", month_label(variable.first_month))
                .replace("%2", month_label(variable.last_month))
            )
            out.append(
                Segment(
                    self.tr("Variable costs"),
                    variable.monthly_cents,
                    [],
                    [period],
                    QBrush(neutral, Qt.BrushStyle.BDiagPattern),
                )
            )
        return out

    def _hover(self, piece: QPieSlice, on: bool, tip: str) -> None:
        piece.setExploded(on)
        piece.setExplodeDistanceFactor(0.04)
        if on:
            QToolTip.showText(QCursor.pos(), tip)
        else:
            QToolTip.hideText()

    def _place_overlays(self) -> None:
        """The total in the middle of the hole, the legend in two columns below."""
        chart = self.view.chart()
        area = chart.plotArea()
        heights = [item.boundingRect().height() for item in self._center]
        top = area.center().y() - sum(heights) / 2
        for item, height in zip(self._center, heights, strict=True):
            item.setPos(area.center().x() - item.boundingRect().width() / 2, top)
            top += height
        if not self._legend:
            return
        rows = math.ceil(len(self._legend) / 2)
        metrics = QFontMetrics(self.font())
        row_height = metrics.height() + LEGEND_ROW_GAP
        column = (chart.size().width() - 2 * LEGEND_SIDE) / 2
        for i, (dot, label, name, share) in enumerate(self._legend):
            x = LEGEND_SIDE + (i // rows) * column  # down the first column, then the second
            y = area.bottom() + 8 + (i % rows) * row_height
            room = column - LEGEND_DOT - 12 - metrics.horizontalAdvance(f"  {share}")
            name = metrics.elidedText(name, Qt.TextElideMode.ElideRight, max(0, int(room)))
            label.setText(f"{name}  {share}")
            dot.setPos(x, y + (metrics.height() - LEGEND_DOT) / 2)
            label.setPos(x + LEGEND_DOT + 6, y)
