"""Top payees report."""

from __future__ import annotations

from datetime import date

from PySide6.QtCharts import (
    QBarCategoryAxis,
    QHorizontalBarSeries,
)
from PySide6.QtCore import Qt, Signal

from hiro_budget.app import App
from hiro_budget.gui.charts import (
    add_bar_tooltips,
    add_bottom_money_axis,
    chart_view,
    expense_color,
    fill_table,
    money_axis,
    money_bar_labels,
    new_bar_set,
    new_chart,
    report_table,
    set_chart,
)
from hiro_budget.gui.reports.base import ReportPage, chart_and_table, fmt_percent, note_label
from hiro_budget.services import (
    reports,
)

TOP_PAYEE_BARS = 12
LABEL_LENGTH = 24  # longer axis labels are elided to '...' by QtCharts


class TopPayeesReport(ReportPage):
    """Where the money goes: expenses per payee."""

    #: (payee name, start, end) - show its bookings on the bookings page
    show_payee = Signal(str, object, object)

    def __init__(self, app: App, parent=None) -> None:
        super().__init__(app, parent)
        self.data: list[reports.PayeeTotal] = []
        self._period: tuple[date, date] | None = None
        self.view = chart_view(new_chart(self.tr("Largest payees")))
        self.table = report_table(
            [
                self.tr("Payee"),
                self.tr("Category"),
                self.tr("Bookings"),
                self.tr("Expenses"),
                self.tr("Average"),
                self.tr("Share"),
            ],
            {2, 3, 4, 5},
        )
        self.table.setToolTip(self.tr("Double-click a payee to show its bookings"))
        self.table.cellDoubleClicked.connect(self._open)
        self.note = note_label()
        self.setLayout(chart_and_table([self.view], self.table, self.note))

    def refresh(
        self, start: date, end: date, account_id: int | None, one_offs: bool = True
    ) -> None:
        self._period = (start, end)
        with self.app.session() as s:
            data = reports.top_payees(s, start, end, account_id, one_offs)
        self.data = data
        total = sum(p.expense_cents for p in data)
        self._draw_chart(data[:TOP_PAYEE_BARS])
        rows = [
            [
                p.name,
                p.category,
                str(p.count),
                p.expense_cents,
                p.average_cents,
                fmt_percent(p.expense_cents / total if total else None),
            ]
            for p in data
        ]
        fill_table(self.table, rows)
        top = sum(p.expense_cents for p in data[:10])
        self.note.setText(
            self.tr(
                "%1 payees. The 10 largest received %2 of the expenses. Card and reference "
                "numbers are ignored, so all bookings of a shop are counted together; "
                "transfers between own accounts and income are not included. Double-click "
                "a payee to show its bookings."
            )
            .replace("%1", str(len(data)))
            .replace("%2", fmt_percent(top / total if total else None, 0))
            + ("" if one_offs else " " + self.tr("Bookings in one-off categories are left out."))
            if data
            else self.tr("No expenses in this period.")
        )

    def _open(self, row: int, _column: int) -> None:
        if self._period is not None and 0 <= row < len(self.data):
            self.show_payee.emit(self.data[row].name, *self._period)

    def _draw_chart(self, payees: list[reports.PayeeTotal]) -> None:
        chart = new_chart(self.tr("Largest payees"))
        chart.legend().hide()
        ordered = list(reversed(payees))  # largest at the top
        labels = []
        for p in ordered:
            label = p.name if len(p.name) <= LABEL_LENGTH else p.name[: LABEL_LENGTH - 1] + "…"
            while label in labels:  # axis labels must be unique
                label += " "
            labels.append(label)
        names = [p.name for p in ordered]  # full names in the tooltips
        bars = new_bar_set(self.tr("Expenses"), expense_color())
        values = [p.expense_cents / 100 for p in ordered]
        for value in values:
            bars.append(value)
        series = QHorizontalBarSeries()
        series.append(bars)
        add_bar_tooltips(series, names)
        axis = QBarCategoryAxis()
        axis.append(labels)
        chart.addSeries(series)
        chart.addAxis(axis, Qt.AlignmentFlag.AlignLeft)
        # room after the largest bar for its amount
        x = money_axis([*values, max(values, default=0) * 1.15])
        add_bottom_money_axis(chart, x)
        series.attachAxis(axis)
        series.attachAxis(x)
        set_chart(self.view, chart)
        self.bar_labels = money_bar_labels(chart, series, values)
