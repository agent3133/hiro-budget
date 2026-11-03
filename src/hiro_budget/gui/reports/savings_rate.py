"""Savings rate report."""

from __future__ import annotations

import math
from datetime import date

from PySide6.QtCharts import (
    QBarSet,
    QStackedBarSeries,
    QValueAxis,
)
from PySide6.QtGui import (
    QCursor,
)
from PySide6.QtWidgets import (
    QToolTip,
)

from hiro_budget.app import App
from hiro_budget.gui.charts import (
    add_line_tooltips,
    attach,
    chart_view,
    expense_color,
    fill_table,
    income_color,
    line_series,
    mark_partial,
    month_axis,
    month_label,
    month_labels,
    new_bar_set,
    new_chart,
    report_table,
    set_chart,
    strip_markup,
)
from hiro_budget.gui.common import (
    accent_color,
    fmt_date,
    money,
)
from hiro_budget.gui.reports.base import (
    ReportPage,
    breakdown_context,
    chart_and_table,
    drill_tip,
    fmt_percent,
    month_range,
    note_label,
    one_off_note,
    open_breakdown,
    shift_note,
)
from hiro_budget.services import (
    budget,
    drilldown,
    reports,
    savings,
)


class SavingsRateReport(ReportPage):
    """Share of the income that was saved, per month and over 12 months."""

    AXIS_LIMIT = 100  # % - months far outside (e.g. a large purchase) are cut off

    def __init__(self, app: App, parent=None) -> None:
        super().__init__(app, parent)
        self.data: savings.SavingsRates | None = None
        self.view = chart_view(new_chart(self.tr("Savings rate")))
        self.table = report_table(
            [
                self.tr("Month"),
                self.tr("Income"),
                self.tr("Expenses"),
                self.tr("Saved"),
                self.tr("Savings rate"),
                self.tr("12 months"),
                self.tr("One-offs"),
            ],
            {1, 2, 3, 4, 5, 6},
        )
        self.table.horizontalHeaderItem(6).setToolTip(
            self.tr("Income minus expenses of one-off categories in the month")
        )
        self.table.setToolTip(drill_tip())
        self.table.cellDoubleClicked.connect(self._drill)
        self._filter: tuple[date, date, int | None, bool] | None = None
        self.note = note_label()
        self.setLayout(chart_and_table([self.view], self.table, self.note))

    def refresh(
        self, start: date, end: date, account_id: int | None, one_offs: bool = True
    ) -> None:
        with self.app.session() as s:
            data = savings.savings_rates(s, start, end, account_id, one_offs)
            shifted = reports.shifted_categories(s)
        self.data = data
        self._filter = (start, end, account_id, one_offs)
        chart = new_chart(self.tr("Savings rate per month"))
        if not data.months:
            set_chart(self.view, chart)
            fill_table(self.table, [])
            self.note.setText(self.tr("No income or expenses in this period."))
            return
        months = [m.month for m in data.months]
        # one line each for tooltips and the table; the axis says it on two lines
        labels = [strip_markup(label) for label in month_labels(months, data.partial)]
        limit = self.AXIS_LIMIT

        def clip(value: float | None) -> float:
            return max(-limit, min(limit, (value or 0) * 100))

        saved = new_bar_set(self.tr("Saved"), income_color())
        overspent = new_bar_set(self.tr("Spent more than earned"), expense_color())
        for m in data.months:
            value = clip(m.rate)
            saved.append(max(value, 0))
            overspent.append(min(value, 0))
        bars = QStackedBarSeries()
        bars.append(saved)
        bars.append(overspent)
        mark_partial([saved, overspent], months, data.partial)  # lighter: data missing

        def bar_hovered(status: bool, index: int, _bar_set: QBarSet) -> None:
            if status:
                m = data.months[index]
                QToolTip.showText(
                    QCursor.pos(),
                    self.tr("%1: savings rate %2 (saved %3 of %4)")
                    .replace("%1", labels[index])
                    .replace("%2", fmt_percent(m.rate))
                    .replace("%3", money(m.saved_cents))
                    .replace("%4", money(m.income_cents)),
                )
            else:
                QToolTip.hideText()

        bars.hovered.connect(bar_hovered)
        for bar_set in (saved, overspent):
            bar_set.doubleClicked.connect(lambda i: self._drill(i, 3))
        rolling_values = [
            None if m.rolling_rate is None else clip(m.rolling_rate) for m in data.months
        ]
        rolling = line_series(self.tr("12 months"), rolling_values, accent_color())
        add_line_tooltips(rolling, labels, unit="%")
        x = month_axis(months, data.partial)
        y = QValueAxis()
        values = [clip(m.rate) for m in data.months] + [v for v in rolling_values if v is not None]
        # steps of 25 % or 50 % (values are clipped to ±limit); both ends are multiples of
        # the step, so 0 % is on a grid line
        low, high = min(-25, min(values)), max(25, max(values))
        step = 25 if (math.ceil(high / 25) - math.floor(low / 25)) <= 8 else 50
        low, high = math.floor(low / step) * step, math.ceil(high / step) * step
        y.setRange(low, high)
        y.setTickCount(int((high - low) / step) + 1)
        y.setLabelFormat("%.0f %%")
        attach(chart, bars, x, y)
        attach(chart, rolling, x, y)
        set_chart(self.view, chart)

        rows = [
            [
                label,
                m.income_cents,
                m.expense_cents,
                m.saved_cents,
                fmt_percent(m.rate),
                fmt_percent(m.rolling_rate),
                m.one_off_net_cents if m.one_off_income_cents or m.one_off_expense_cents else "",
            ]
            for m, label in zip(data.months, labels, strict=True)
        ]
        one_off_income, one_off_expenses = data.one_off_income_cents, data.one_off_expense_cents
        rows.append(
            [
                self.tr("Period"),
                data.income_cents,
                data.income_cents - data.saved_cents,
                data.saved_cents,
                fmt_percent(data.rate),
                "",
                one_off_income - one_off_expenses,
            ]
        )
        fill_table(self.table, rows, bold_last=True, signed=frozenset({3, 6}))
        self.table.setColumnHidden(6, not (one_off_income or one_off_expenses))
        notes = [
            self.tr(
                "Savings rate = (income − expenses) / income. Transfers between own accounts "
                "are no expenses: investments and loan repayments count as saved, interest "
                "as an expense. The 12-month rate is saved / income of up to 12 months."
            )
        ]
        if data.complete_from and data.complete_from > months[0]:
            notes.append(
                self.tr("It starts in %1, the first month with data on all accounts.").replace(
                    "%1", month_label(data.complete_from)
                )
            )
        if data.partial is not None:
            notes.append(
                self.tr(
                    "%1 has data only up to %2 (lighter bar, no 12-month rate): its savings "
                    "rate is not comparable yet."
                )
                .replace("%1", month_label(data.partial[0]))
                .replace("%2", fmt_date(data.partial[1]))
            )
        elif months[-1] == f"{date.today():%Y-%m}":
            notes.append(self.tr("The current month is not complete yet."))
        if any(abs((m.rate or 0) * 100) > limit for m in data.months):
            notes.append(
                self.tr("Bars beyond ±%1 % are cut off; the table shows the values.").replace(
                    "%1", str(limit)
                )
            )
        notes.append(shift_note(shifted))
        notes.append(one_off_note(one_offs, one_off_expenses, one_off_income))
        self.note.setText(" ".join(n for n in notes if n))

    def _drill(self, row: int, column: int) -> None:
        """Income, expenses, saved (also via month and rates) or one-offs of a month or of
        the period row."""
        if self.data is None or self._filter is None or column == 5:
            return
        start, end, account_id, one_offs = self._filter
        months = self.data.months
        if row < len(months):
            label = month_label(months[row].month)
            first, last = month_range(months[row].month, None, end)
        elif months:
            label = self.tr("Period")
            first, last = budget.month_first_day(months[0].month), end
        else:
            return
        measure: drilldown.Measure = {1: "income", 2: "expenses"}.get(column, "saved")
        only_one_offs = column == 6
        names = {
            "income": self.tr("Income"),
            "expenses": self.tr("Expenses"),
            "saved": self.tr("Saved"),
        }
        title = self.tr("One-offs") if only_one_offs else names[measure]
        open_breakdown(
            self,
            self.app,
            f"{title} · {label}",
            breakdown_context(
                self.app, first, last, account_id, None if only_one_offs else one_offs
            ),
            lambda s: drilldown.report_breakdown(
                s,
                measure,
                first,
                last,
                account_id,
                True if only_one_offs else one_offs,
                only_one_offs=only_one_offs,
            ),
        )


# --- cash flow -------------------------------------------------------------------------
