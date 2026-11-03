"""Mortgage report."""

from __future__ import annotations

from datetime import date

from PySide6.QtCharts import (
    QStackedBarSeries,
)
from PySide6.QtCore import Qt
from PySide6.QtGui import (
    QColor,
    QPen,
)
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QSpinBox,
)

from hiro_budget.app import App
from hiro_budget.gui import theme
from hiro_budget.gui.charts import (
    add_bar_tooltips,
    add_line_tooltips,
    attach,
    chart_view,
    expense_color,
    fill_table,
    line_series,
    money_axis,
    month_axis,
    month_label,
    new_bar_set,
    new_chart,
    report_table,
    set_chart,
    sparse_month_axis,
)
from hiro_budget.gui.common import (
    accent_color,
    money,
)
from hiro_budget.gui.reports.base import ReportPage, chart_and_table, fmt_percent, note_label
from hiro_budget.services import (
    mortgage_report,
)


def _month_year(month: str) -> str:
    """ "2038-07" -> "07/2038"."""
    return f"{month[5:]}/{month[:4]}"


class MortgageReport(ReportPage):
    """Remaining debt and what was paid: interest, repayment, special repayments."""

    def __init__(self, app: App, parent=None) -> None:
        super().__init__(app, parent)
        self.data: mortgage_report.Mortgage | None = None
        self.debt_view = chart_view(new_chart(self.tr("Remaining debt")))
        self.view = chart_view(new_chart(self.tr("Payments per month")))
        self.table = report_table(
            [
                self.tr("Year"),
                self.tr("Interest"),
                self.tr("Repayment"),
                self.tr("Special repayments"),
                self.tr("Total"),
                self.tr("Remaining debt"),
            ],
            {1, 2, 3, 4, 5},
        )
        self.note = note_label()
        # the forecast (#47): what a special repayment every year would bring
        self.extra = QSpinBox()
        self.extra.setRange(0, 1_000_000)
        self.extra.setSingleStep(500)
        self.extra.setSuffix(" €")
        self.extra.setGroupSeparatorShown(True)
        self.extra.setValue(int(app.ui_state.get("reports.mortgage_extra", 0) or 0))
        self.extra.setToolTip(
            self.tr("Paid every December on top of the instalments; changes the forecast")
        )
        self.extra.valueChanged.connect(self._extra_changed)
        self.forecast: mortgage_report.Forecast | None = None
        self.baseline: mortgage_report.Forecast | None = None  # without special repayments
        controls = QHBoxLayout()
        controls.addWidget(QLabel(self.tr("Special repayment per year")))
        controls.addWidget(self.extra)
        controls.addStretch(1)
        layout = chart_and_table([self.debt_view, self.view], self.table, self.note)
        layout.insertLayout(0, controls)
        self.setLayout(layout)

    def _extra_changed(self, value: int) -> None:
        self.app.ui_state.set("reports.mortgage_extra", value)
        if self.data is not None and self.data.months:
            self._draw_debt()
            self._write_note()

    def _fixed_until(self) -> date | None:
        """The end of the fixed-rate period of the shown loan, if configured."""
        if self.data is None or len(self.data.loans) != 1:
            return None
        return next(
            (
                m.fixed_until
                for m in self.app.settings.app.mortgage
                if m.loan_account == self.data.loans[0]
            ),
            None,
        )

    def _draw_debt(self) -> None:
        """The remaining debt per month end, continued dashed by the forecast (one loan)."""
        data = self.data
        debt_chart = new_chart(self.tr("Remaining debt at the end of each month"))
        debt_chart.legend().hide()
        self.forecast = self.baseline = None
        if len(data.loans) == 1:  # loans with other rates cannot be forecast as one
            fixed = self._fixed_until()
            self.forecast = mortgage_report.forecast(data, self.extra.value() * 100, fixed)
            self.baseline = mortgage_report.forecast(data, 0, fixed)
        history = [m.month for m in data.months]
        future = [m for m, _ in self.forecast.months if m > history[-1]] if self.forecast else []
        months = history + future
        labels = [month_label(m) for m in months]
        known = [m for m in data.months if m.debt is not None]
        values = [m.debt / 100 for m in known]
        if known:
            debt = line_series(
                self.tr("Remaining debt"),
                [m.debt / 100 if m.debt is not None else None for m in data.months],
                expense_color(),
            )
            debt.setPointsVisible(len(months) <= 36)
            add_line_tooltips(debt, labels)
            x_axis = sparse_month_axis(months)
            if self.forecast is not None:
                start = history.index(self.forecast.start)
                start_debt = data.months[start].debt / 100
                points = [None] * start + [start_debt]
                points += [None] * (len(history) - start - 1)
                points += [cents / 100 for m, cents in self.forecast.months if m > history[-1]]
                ahead = line_series(self.tr("Forecast"), points, expense_color())
                pen = QPen(ahead.pen())
                pen.setStyle(Qt.PenStyle.DashLine)
                ahead.setPen(pen)
                ahead.setPointsVisible(False)
                add_line_tooltips(ahead, labels)
                values += [p for p in points if p is not None]
                attach(debt_chart, ahead, x_axis, money_axis(values))
                y_axis = next(a for a in debt_chart.axes() if a is not x_axis)
            else:
                y_axis = money_axis(values, tight=True)
            attach(debt_chart, debt, x_axis, y_axis)
        set_chart(self.debt_view, debt_chart)

    def _forecast_note(self) -> str:
        f, base = self.forecast, self.baseline
        if self.data is not None and len(self.data.loans) > 1:
            return self.tr("Choose one loan account above to see a forecast.")
        if f is None or base is None:
            return ""
        text = (
            self.tr("Forecast from the instalment of %1: %2 a month at %3 interest a year.")
            .replace("%1", month_label(f.start))
            .replace("%2", money(f.payment))
            .replace("%3", fmt_percent(f.rate, 2))
        )
        if base.payoff is None:
            return text + " " + self.tr("Not paid off within 60 years at this instalment.")
        text += " " + (
            self.tr("Paid off in %1; interest still to pay %2.")
            .replace("%1", _month_year(base.payoff))
            .replace("%2", money(base.interest))
        )
        if base.fixed_until is not None and base.debt_at_fixed_end is not None:
            text += " " + (
                self.tr("Remaining debt at the end of the fixed-rate period (%1): %2.")
                .replace("%1", _month_year(base.fixed_until))
                .replace("%2", money(base.debt_at_fixed_end))
            )
        if f.extra_per_year and f.payoff is not None:
            saved = len(base.months) - len(f.months)
            text += " " + (
                self.tr(
                    "With %1 special repayment every December: paid off in %2, %3 earlier, "
                    "%4 less interest."
                )
                .replace("%1", money(f.extra_per_year))
                .replace("%2", _month_year(f.payoff))
                .replace("%3", self._duration(saved))
                .replace("%4", money(base.interest - f.interest))
            )
            if f.debt_at_fixed_end is not None:
                text += " " + self.tr(
                    "Remaining debt then at the end of the fixed-rate period: %1."
                ).replace("%1", money(f.debt_at_fixed_end))
        return text

    def _duration(self, months: int) -> str:
        years, rest = divmod(months, 12)
        if not years:
            return self.tr("%n month(s)", "", rest)
        if not rest:
            return self.tr("%n year(s)", "", years)
        return (
            self.tr("%1 and %2")
            .replace("%1", self.tr("%n year(s)", "", years))
            .replace("%2", self.tr("%n month(s)", "", rest))
        )

    def refresh(
        self, start: date, end: date, account_id: int | None, one_offs: bool = True
    ) -> None:
        with self.app.session() as s:
            data = mortgage_report.mortgage_progress(
                s, self.app.settings, start, min(end, date.today()), account_id
            )
        self.data = data
        debt_chart = new_chart(self.tr("Remaining debt at the end of each month"))
        debt_chart.legend().hide()
        chart = new_chart(self.tr("Payments per month"))
        if not data.months:
            set_chart(self.debt_view, debt_chart)
            set_chart(self.view, chart)
            fill_table(self.table, [])
            self.note.setText(
                self.tr("No loan account with bookings in this period.")
                if not data.loans
                else self.tr("No mortgage payments in this period.")
            )
            return
        months = [m.month for m in data.months]
        labels = [month_label(m) for m in months]
        self._draw_debt()

        parts = [
            (self.tr("Interest"), [m.interest for m in data.months], expense_color()),
            (
                self.tr("Repayment"),
                [m.principal for m in data.months],
                QColor(theme.color("principal")),
            ),
            (self.tr("Special repayments"), [m.extra for m in data.months], accent_color()),
        ]
        bars = QStackedBarSeries()
        for name, values, color in parts:
            bar_set = new_bar_set(name, color)
            for cents in values:
                bar_set.append(cents / 100)
            bars.append(bar_set)
        add_bar_tooltips(bars, labels)
        attach(chart, bars, month_axis(months), money_axis([m.total / 100 for m in data.months]))
        set_chart(self.view, chart)

        years = data.years()
        rows = [
            [str(year), y.interest, y.principal, y.extra, y.total, y.debt if y.debt else "–"]
            for year, y in years
        ]
        total = mortgage_report.MonthPayments(
            "",
            sum(y.interest for _, y in years),
            sum(y.principal for _, y in years),
            sum(y.extra for _, y in years),
        )
        rows.append(
            [self.tr("Total"), total.interest, total.principal, total.extra, total.total, ""]
        )
        fill_table(self.table, rows, bold_last=True)

        self._write_note()

    def _write_note(self) -> None:
        data = self.data
        notes = [
            self.tr(
                "Loan: %1. Payments by booking date of the paying account: an instalment "
                "booked at the turn of a month can fall into the next one. Repayment = "
                "principal part of the instalments, special repayments = other transfers "
                "to the loan account."
            ).replace("%1", ", ".join(data.loans))
        ]
        last = next((m for m in reversed(data.months) if m.interest and m.principal), None)
        if last is not None:
            share = last.interest / (last.interest + last.principal)
            notes.append(
                self.tr("Latest instalment (%1): %2 interest.")
                .replace("%1", month_label(last.month))
                .replace("%2", fmt_percent(share, 0))
            )
        if forecast := self._forecast_note():
            notes.append(forecast)
        self.note.setText(" ".join(notes))


# --- depot -----------------------------------------------------------------------------
