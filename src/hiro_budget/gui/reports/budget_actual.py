"""Budget vs. actual report."""

from __future__ import annotations

from datetime import date

from PySide6.QtCharts import (
    QBarCategoryAxis,
    QBarSeries,
    QHorizontalBarSeries,
)
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
)

from hiro_budget.app import App
from hiro_budget.gui.charts import (
    add_bar_tooltips,
    add_bottom_money_axis,
    attach,
    chart_view,
    expense_color,
    fill_table,
    money_axis,
    month_axis,
    month_label,
    new_bar_set,
    new_chart,
    one_off_name,
    report_table,
    set_chart,
)
from hiro_budget.gui.common import (
    accent_color,
)
from hiro_budget.gui.reports.base import (
    ReportPage,
    chart_and_table,
    drill_tip,
    fmt_percent,
    note_label,
    one_off_note,
    open_breakdown,
    shift_note,
)
from hiro_budget.services import (
    budget_report,
    drilldown,
    reports,
)


class BudgetActualReport(ReportPage):
    """Assigned vs. spent per month and per budget line."""

    def __init__(self, app: App, parent=None) -> None:
        super().__init__(app, parent)
        self.data: budget_report.BudgetActual | None = None
        self._period: tuple[date, date] | None = None
        self._one_offs = True
        self.only_budgeted = QCheckBox(self.tr("Only months with a budget"))
        self.only_budgeted.setChecked(True)
        self.only_budgeted.setToolTip(
            self.tr("Leave out months in which nothing was assigned (before budgeting began).")
        )
        self.only_budgeted.toggled.connect(self._redraw)
        self.view = chart_view(new_chart(self.tr("Assigned and spent per month")))
        self.lines_view = chart_view(new_chart(self.tr("Largest categories")))
        self.table = report_table(
            [
                self.tr("Category"),
                self.tr("Assigned"),
                self.tr("Spent"),
                self.tr("Difference"),
                self.tr("Used"),
            ],
            {1, 2, 3, 4},
        )
        self.table.setToolTip(drill_tip())
        self.table.cellDoubleClicked.connect(self._drill)
        self.note = note_label()
        layout = chart_and_table([self.view, self.lines_view], self.table, self.note)
        layout.insertWidget(0, self.only_budgeted)
        self.setLayout(layout)

    def refresh(
        self, start: date, end: date, account_id: int | None, one_offs: bool = True
    ) -> None:
        self._period = (start, end)  # the budget covers all budget accounts
        self._one_offs = one_offs
        self._redraw()

    def _redraw(self) -> None:
        if self._period is None:
            return
        start, end = self._period
        last = min(f"{end:%Y-%m}", f"{date.today():%Y-%m}")
        with self.app.session() as s:
            data = budget_report.budget_vs_actual(
                s,
                self.app.settings,
                f"{start:%Y-%m}",
                last,
                self.only_budgeted.isChecked(),
                self._one_offs,
            )
            shifted = reports.shifted_categories(s)
        self.data = data
        chart = new_chart(self.tr("Assigned and spent per month"))
        set_chart(self.lines_view, new_chart(self.tr("Largest categories")))
        if not data.months:
            set_chart(self.view, chart)
            fill_table(self.table, [])
            self.note.setText(self.tr("No budget in this period."))
            return
        months = [m.month for m in data.months]
        assigned = new_bar_set(self.tr("Assigned"), accent_color())
        spent = new_bar_set(self.tr("Spent"), expense_color())
        for m in data.months:
            assigned.append(m.assigned / 100)
            spent.append(m.spent / 100)
        series = QBarSeries()
        series.append(assigned)
        series.append(spent)
        add_bar_tooltips(series, [month_label(m) for m in months])
        spent.doubleClicked.connect(lambda i: self._drill_spent([months[i]], data.category_ids))
        values = [m.assigned / 100 for m in data.months] + [m.spent / 100 for m in data.months]
        attach(chart, series, month_axis(months), money_axis(values))
        set_chart(self.view, chart)

        self._draw_lines(data.lines)

        def used(line: budget_report.LineActual) -> str:
            return fmt_percent(line.used, 0)

        rows = [
            [self._line_name(line), line.assigned, line.spent, line.difference, used(line)]
            for line in data.lines
        ]
        total = budget_report.LineActual("", self.tr("Total"), "", False, data.assigned, data.spent)
        rows.append([total.name, total.assigned, total.spent, total.difference, used(total)])
        fill_table(self.table, rows, bold_last=True, signed=frozenset({3}))
        overspent = [line for line in data.lines if line.difference < 0]
        notes = [
            self.tr(
                "Months: %1 (%2 – %3). Difference = assigned − spent: negative means more was "
                "spent than assigned (%4 categories). Money from earlier months is not "
                "counted. The budget covers all budget accounts; the account filter does not "
                "apply."
            )
            .replace("%1", str(len(months)))
            .replace("%2", month_label(months[0]))
            .replace("%3", month_label(months[-1]))
            .replace("%4", str(len(overspent))),
            shift_note(shifted),
            one_off_note(self._one_offs, data.one_off_spent),
        ]
        self.note.setText(" ".join(n for n in notes if n))

    def _line_name(self, line: budget_report.LineActual) -> str:
        if line.is_group:
            return self.tr("%1 (whole group)").replace("%1", line.name)
        return one_off_name(f"{line.group_name}: {line.name}", line.one_off)

    def _drill(self, row: int, column: int) -> None:
        """Spent (or difference / used) of a line or the total: its spending."""
        if self.data is None or column < 2:
            return
        months = [m.month for m in self.data.months]
        if row < len(self.data.lines):
            line = self.data.lines[row]
            self._drill_spent(months, line.category_ids, self._line_name(line))
        else:
            self._drill_spent(months, self.data.category_ids)

    def _drill_spent(self, months: list[str], category_ids, name: str | None = None) -> None:
        if not months or self._period is None:
            return
        title = self.tr("Spent: %1").replace("%1", name) if name else self.tr("Spent")
        if len(months) == 1:
            title += f" · {month_label(months[0])}"
        context = (
            f"{month_label(months[0])} – {month_label(months[-1])} · "
            + self.tr("budget accounts")
            + " · "
            + (self.tr("with one-offs") if self._one_offs else self.tr("without one-offs"))
        )
        settings = self.app.settings
        open_breakdown(
            self,
            self.app,
            title,
            context,
            lambda s: drilldown.budget_breakdown(s, settings, months, category_ids),
            budget_rules=True,
        )

    def _draw_lines(self, lines: list[budget_report.LineActual]) -> None:
        """Assigned and spent of the largest budget lines, side by side."""
        chart = new_chart(self.tr("Largest categories"))
        largest = sorted(lines, key=lambda line: max(line.assigned, line.spent), reverse=True)[:10]
        largest.reverse()  # largest at the top of a horizontal bar chart
        names = [line.name for line in largest]
        labels = [
            one_off_name(
                f"{line.group_name}: {line.name}" if names.count(line.name) > 1 else line.name,
                line.one_off,
            )
            for line in largest
        ]
        assigned = new_bar_set(self.tr("Assigned"), accent_color())
        spent = new_bar_set(self.tr("Spent"), expense_color())
        for line in largest:
            assigned.append(line.assigned / 100)
            spent.append(max(line.spent, 0) / 100)
        series = QHorizontalBarSeries()
        series.append(assigned)
        series.append(spent)
        add_bar_tooltips(series, labels)
        months = [m.month for m in self.data.months] if self.data else []
        spent.doubleClicked.connect(
            lambda i: self._drill_spent(
                months, largest[i].category_ids, self._line_name(largest[i])
            )
        )
        axis = QBarCategoryAxis()
        axis.append(labels)
        values = [line.assigned / 100 for line in largest] + [line.spent / 100 for line in largest]
        chart.addSeries(series)
        chart.addAxis(axis, Qt.AlignmentFlag.AlignLeft)
        x = money_axis(values)
        add_bottom_money_axis(chart, x)
        series.attachAxis(axis)
        series.attachAxis(x)
        set_chart(self.lines_view, chart)


# --- savings rate ----------------------------------------------------------------------
