"""Securities account report."""

from __future__ import annotations

from datetime import date

from PySide6.QtCharts import (
    QBarCategoryAxis,
    QStackedBarSeries,
)
from PySide6.QtGui import (
    QColor,
)
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
)

from hiro_budget.app import App
from hiro_budget.gui import theme
from hiro_budget.gui.charts import (
    add_bar_tooltips,
    add_line_tooltips,
    attach,
    chart_view,
    fill_table,
    line_series,
    money_axis,
    new_bar_set,
    new_chart,
    report_table,
    set_chart,
)
from hiro_budget.gui.common import (
    amount_color,
    fmt_date,
    money,
)
from hiro_budget.gui.reports.base import ReportPage, chart_and_table, note_label
from hiro_budget.services import (
    depot_report,
)


class DepotReport(ReportPage):
    """Value per security on each depot statement, and the money invested."""

    def __init__(self, app: App, parent=None) -> None:
        super().__init__(app, parent)
        self.data: depot_report.DepotHistory | None = None
        self._period: tuple[date, date] | None = None
        self.depot = QComboBox()
        self.depot.currentIndexChanged.connect(self._redraw)
        self.depot_label = QLabel(self.tr("Depot"))
        controls = QHBoxLayout()
        controls.addWidget(self.depot_label)
        controls.addWidget(self.depot)
        controls.addStretch(1)
        self.view = chart_view(new_chart(self.tr("Value per depot statement")))
        self.table = report_table([self.tr("Security")], set())
        self.note = note_label()
        layout = chart_and_table([self.view], self.table, self.note)
        layout.insertLayout(0, controls)
        self.setLayout(layout)

    def refresh(
        self, start: date, end: date, account_id: int | None, one_offs: bool = True
    ) -> None:
        self._period = (start, end)
        with self.app.session() as s:
            depots = [(a.id, a.name) for a in depot_report.depot_accounts(s)]
        self.depot.blockSignals(True)
        chosen = any(depot_id == account_id for depot_id, _ in depots)
        previous = account_id if chosen else self.depot.currentData()
        self.depot.clear()
        for depot_id, name in depots:
            self.depot.addItem(name, depot_id)
        self.depot.setCurrentIndex(max(0, self.depot.findData(previous)))
        self.depot.blockSignals(False)
        for w in (self.depot, self.depot_label):
            w.setVisible(len(depots) > 1)  # only a choice with several depots
        self._redraw()

    def _redraw(self) -> None:
        depot_id = self.depot.currentData()
        chart = new_chart(self.tr("Value per depot statement"))
        if depot_id is None or self._period is None:
            set_chart(self.view, chart)
            fill_table(self.table, [])
            self.note.setText(self.tr("No securities account."))
            return
        with self.app.session() as s:
            data = depot_report.depot_history(s, depot_id, *self._period)
        self.data = data
        if not data.statements:
            set_chart(self.view, chart)
            fill_table(self.table, [])
            self.note.setText(self.tr("No depot statement in this period."))
            return
        labels = [fmt_date(st.day) for st in data.statements]
        names = data.names()
        bars = QStackedBarSeries()
        for i, name in enumerate(names):
            bar_set = new_bar_set(name, QColor(theme.series_color(i)))
            for st in data.statements:
                bar_set.append(st.securities.get(name, 0) / 100)
            bars.append(bar_set)
        add_bar_tooltips(bars, labels)
        invested = line_series(
            self.tr("Invested"),
            [st.invested / 100 for st in data.statements],
            QColor(theme.color("line_series")),
        )
        add_line_tooltips(invested, labels)
        x = QBarCategoryAxis()
        x.append(labels)
        values = [st.value / 100 for st in data.statements]
        values += [st.invested / 100 for st in data.statements]
        y = money_axis(values)
        attach(chart, bars, x, y)
        attach(chart, invested, x, y)
        set_chart(self.view, chart)

        headers = [self.tr("Security"), *labels]
        self.table.setColumnCount(len(headers))
        self.table.setHorizontalHeaderLabels(headers)
        self.table.setProperty("numeric", list(range(1, len(headers))))
        rows = [[name, *[st.securities.get(name, 0) for st in data.statements]] for name in names]
        rows.append([self.tr("Value"), *[st.value for st in data.statements]])
        rows.append([self.tr("Invested"), *[st.invested for st in data.statements]])
        rows.append([self.tr("Gain"), *[st.gain for st in data.statements]])
        fill_table(self.table, rows, bold_last=True)
        gain_row = len(rows) - 1  # coloured by sign, the value rows above are not
        for col in range(1, len(headers)):
            cents = data.statements[col - 1].gain
            if cents:
                self.table.item(gain_row, col).setForeground(amount_color(cents))
        first, last = data.statements[0], data.statements[-1]
        self.note.setText(
            self.tr(
                "Market value on each depot statement. Invested = value on %1 plus purchases "
                "minus sales since; the difference is the price gain (%2 up to %3). "
                "Distributions are paid to another account and not included."
            )
            .replace("%1", fmt_date(first.day))
            .replace("%2", money(last.gain))
            .replace("%3", fmt_date(last.day))
        )


# --- top payees ------------------------------------------------------------------------
