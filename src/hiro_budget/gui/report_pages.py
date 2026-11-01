"""Report pages shown as tabs on the reports page (besides the overview).

Each page gets the period, account and the one-off switch of the reports page's filter
bar through ``refresh(start, end, account_id, one_offs)``.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date

from PySide6.QtCharts import (
    QAbstractBarSeries,
    QBarCategoryAxis,
    QBarSeries,
    QBarSet,
    QHorizontalBarSeries,
    QPieSeries,
    QPieSlice,
    QStackedBarSeries,
    QValueAxis,
)
from PySide6.QtCore import QT_TRANSLATE_NOOP, QCoreApplication, QLocale, QMargins, Qt, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QCursor,
    QFont,
    QFontMetrics,
    QIcon,
    QPainter,
    QPalette,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFrame,
    QGraphicsEllipseItem,
    QGraphicsSimpleTextItem,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from hiro_budget.app import App
from hiro_budget.db.models import Account
from hiro_budget.gui.breakdown_dialog import BreakdownDialog
from hiro_budget.gui.charts import (
    add_bar_tooltips,
    add_line_tooltips,
    attach,
    chart_view,
    expense_color,
    fill_table,
    income_color,
    line_series,
    money_axis,
    month_axis,
    month_label,
    new_chart,
    one_off_name,
    report_table,
    set_chart,
)
from hiro_budget.gui.common import (
    accent_color,
    amount_color,
    fmt_date,
    fmt_number,
    group_colors,
    is_dark,
    money,
)
from hiro_budget.gui.sankey import SankeyChart, SankeyLink, SankeyNode
from hiro_budget.services import (
    budget,
    budget_report,
    cash_flow,
    depot_report,
    drilldown,
    mortgage_report,
    networth,
    recurring,
    reports,
    savings,
)


class ReportPage(QWidget):
    """Base class: a report that follows the filter bar of the reports page."""

    #: open a booking on the bookings page (from a breakdown)
    show_booking = Signal(int)

    def __init__(self, app: App, parent=None) -> None:
        super().__init__(parent)
        self.app = app
        self.breakdown_dialog: BreakdownDialog | None = None  # the last one opened

    def refresh(
        self, start: date, end: date, account_id: int | None, one_offs: bool = True
    ) -> None:
        raise NotImplementedError


def month_range(month: str, start: date | None = None, end: date | None = None):
    """First and last day of a month, limited to a period."""
    first, last = budget.month_first_day(month), budget.month_last_day(month)
    return max(first, start) if start else first, min(last, end) if end else last


def breakdown_context(
    app: App, start: date, end: date, account_id: int | None, one_offs: bool | None
) -> str:
    """Period, account and the one-off switch, as the line under a breakdown's title."""
    parts = [f"{fmt_date(start)} – {fmt_date(end)}"]
    if account_id is not None:
        with app.session() as s:
            account = s.get(Account, account_id)
        if account is not None:
            parts.append(
                QCoreApplication.translate("report_pages", "Account: %1").replace(
                    "%1", account.name
                )
            )
    else:
        parts.append(QCoreApplication.translate("report_pages", "All accounts"))
    if one_offs is not None:
        parts.append(
            QCoreApplication.translate("report_pages", "with one-offs")
            if one_offs
            else QCoreApplication.translate("report_pages", "without one-offs")
        )
    return " · ".join(parts)


def open_breakdown(
    owner: QWidget,
    app: App,
    title: str,
    context: str,
    fetch: Callable[..., drilldown.Breakdown],
    budget_rules: bool = False,
) -> BreakdownDialog:
    """Show how a number adds up; ``owner`` needs a ``show_booking`` signal."""
    with app.session() as s:
        data = fetch(s)
    note = (
        QCoreApplication.translate(
            "report_pages",
            "Spending on budget accounts, also transfers with a category (e.g. the principal "
            "of a mortgage).",
        )
        if budget_rules
        else QCoreApplication.translate(
            "report_pages", "Transfers between own accounts are not counted."
        )
    )
    dialog = BreakdownDialog(title, context, data, [note], owner)
    dialog.show_booking.connect(owner.show_booking.emit)
    owner.breakdown_dialog = dialog
    dialog.open()
    return dialog


DRILL_TIP = QT_TRANSLATE_NOOP("report_pages", "Double-click a number to see how it adds up")


def drill_tip() -> str:
    return QCoreApplication.translate("report_pages", DRILL_TIP)


def note_label() -> QLabel:
    label = QLabel()
    label.setWordWrap(True)
    return label


def one_off_note(included: bool, expenses: int, income: int | None = None) -> str:
    """What one-off categories amount to in a report ("" when there are none); ``income``
    None for reports of expenses only."""
    if not expenses and not income:
        return ""
    if income is None:
        text = (
            QCoreApplication.translate("report_pages", "Includes one-off expenses of %2.")
            if included
            else QCoreApplication.translate(
                "report_pages", "Without one-offs: expenses of %2 are left out."
            )
        )
    else:
        text = (
            QCoreApplication.translate("report_pages", "Includes one-offs: income %1, expenses %2.")
            if included
            else QCoreApplication.translate(
                "report_pages", "Without one-offs: income %1 and expenses %2 are left out."
            )
        )
    return text.replace("%1", money(income or 0)).replace("%2", money(expenses))


def shift_note(shifted: list[tuple[str, int]]) -> str:
    """Which categories count early bookings for the previous month ("" for none)."""
    if not shifted:
        return ""
    names = ", ".join(
        QCoreApplication.translate("report_pages", "%1 (day 1–%2)")
        .replace("%1", name)
        .replace("%2", str(days))
        for name, days in shifted
    )
    return QCoreApplication.translate(
        "report_pages", "Bookings early in a month count for the previous month: %1."
    ).replace("%1", names)


PAGE_TOP_MARGIN = 8  # between the tab bar and a page; no side margins: cards and tables
# line up with the tab bar and the filter bar


def chart_and_table(charts: list[QWidget], table, note: QLabel) -> QVBoxLayout:
    """Charts side by side above, the table below, a note at the bottom."""
    row = QHBoxLayout()
    for chart in charts:
        row.addWidget(chart, 1)
    layout = QVBoxLayout()
    layout.setContentsMargins(0, PAGE_TOP_MARGIN, 0, 0)
    layout.addLayout(row, 3)
    layout.addWidget(table, 2)
    layout.addWidget(note)
    return layout


# --- net worth -------------------------------------------------------------------------


class NetWorthReport(ReportPage):
    """Assets, debts and net worth at each month end; balances per account."""

    def __init__(self, app: App, parent=None) -> None:
        super().__init__(app, parent)
        self.data: networth.NetWorth | None = None
        self.view = chart_view(new_chart(self.tr("Net worth")))
        self.parts_view = chart_view(new_chart(self.tr("Assets and debts")))
        self.table = report_table(
            [self.tr("Account"), self.tr("Start"), self.tr("End"), self.tr("Change")],
            {1, 2, 3},
        )
        self.note = note_label()
        self.setLayout(chart_and_table([self.view, self.parts_view], self.table, self.note))

    def refresh(
        self, start: date, end: date, account_id: int | None, one_offs: bool = True
    ) -> None:
        with self.app.session() as s:
            data = networth.net_worth(s, f"{end:%Y-%m}", account_id)
        months = [m for m in data.months if m >= f"{start:%Y-%m}"]
        self.data = data
        chart = new_chart(self.tr("Net worth at the end of each month"))
        parts = new_chart(self.tr("Assets and debts"))
        set_chart(self.parts_view, parts)
        if not months:
            set_chart(self.view, chart)
            fill_table(self.table, [])
            self.note.setText(self.tr("No account balances in this period."))
            return
        labels = [month_label(m) for m in months]
        # Net worth on its own axis, fitted to its values: changes stay visible.
        net = [data.net(m) / 100 for m in months]
        line = line_series(self.tr("Net worth"), net, accent_color())
        add_line_tooltips(line, labels)
        attach(chart, line, month_axis(months), money_axis(net, tight=True))
        chart.legend().hide()
        set_chart(self.view, chart)
        series = [
            (self.tr("Assets"), [data.assets(m) / 100 for m in months], income_color()),
            (self.tr("Debts"), [-data.debts(m) / 100 for m in months], expense_color()),
        ]
        x = month_axis(months)
        y = money_axis([v for _, values, _ in series for v in values])
        for name, values, color in series:
            line = line_series(name, values, color)
            add_line_tooltips(line, labels)
            attach(parts, line, x, y)

        first, last = months[0], months[-1]

        def balance(a: networth.AccountHistory, month: str) -> int | str:
            """An estimated balance (carried back) with "≈" before it."""
            if a.estimated(month):
                return f"≈ {money(a.value(month))}"
            cents = a.balances.get(month)
            return cents if cents is not None else "–"

        rows = [
            [a.name, balance(a, first), balance(a, last), a.value(last) - a.value(first)]
            for a in data.accounts
        ]
        rows.append(
            [
                self.tr("Net worth"),
                data.net(first),
                data.net(last),
                data.net(last) - data.net(first),
            ]
        )
        fill_table(self.table, rows, bold_last=True)
        notes = [
            self.tr(
                "Balances at the end of %1 and %2. Cash counts as 0 before it was tracked. "
                "Securities: value of the latest depot statement plus purchases and sales "
                "since (no price changes in between)."
            )
            .replace("%1", month_label(first))
            .replace("%2", month_label(last))
        ]
        estimated = data.estimated(months)
        if estimated:
            notes.append(
                self.tr(
                    "≈ estimated: before its first known balance an account counts with it (%1)."
                ).replace(
                    "%1",
                    ", ".join(
                        self.tr("%1 before %2")
                        .replace("%1", a.name)
                        .replace("%2", month_label(known))
                        for a, known in estimated
                    ),
                )
            )
        self.note.setText(" ".join(notes))


# --- budget vs. actual -----------------------------------------------------------------


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
        assigned = QBarSet(self.tr("Assigned"))
        assigned.setColor(accent_color())
        spent = QBarSet(self.tr("Spent"))
        spent.setColor(expense_color())
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
            return _percent(line.used, 0)

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
        assigned = QBarSet(self.tr("Assigned"))
        assigned.setColor(accent_color())
        spent = QBarSet(self.tr("Spent"))
        spent.setColor(expense_color())
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
        chart.addAxis(x, Qt.AlignmentFlag.AlignBottom)
        series.attachAxis(axis)
        series.attachAxis(x)
        set_chart(self.lines_view, chart)


# --- savings rate ----------------------------------------------------------------------


def _percent(value: float | None, decimals: int = 1) -> str:
    """Share as a percentage in the UI language (71,9 % / 71.9 %)."""
    if value is None:
        return "–"
    return f"{QLocale().toString(value * 100, 'f', decimals)} %"


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
        labels = [month_label(m) for m in months]
        limit = self.AXIS_LIMIT

        def clip(value: float | None) -> float:
            return max(-limit, min(limit, (value or 0) * 100))

        saved = QBarSet(self.tr("Saved"))
        saved.setColor(income_color())
        overspent = QBarSet(self.tr("Spent more than earned"))
        overspent.setColor(expense_color())
        for m in data.months:
            value = clip(m.rate)
            saved.append(max(value, 0))
            overspent.append(min(value, 0))
        bars = QStackedBarSeries()
        bars.append(saved)
        bars.append(overspent)

        def bar_hovered(status: bool, index: int, _bar_set: QBarSet) -> None:
            if status:
                m = data.months[index]
                QToolTip.showText(
                    QCursor.pos(),
                    self.tr("%1: savings rate %2 (saved %3 of %4)")
                    .replace("%1", labels[index])
                    .replace("%2", _percent(m.rate))
                    .replace("%3", money(m.saved_cents))
                    .replace("%4", money(m.income_cents)),
                )
            else:
                QToolTip.hideText()

        bars.hovered.connect(bar_hovered)
        for bar_set in (saved, overspent):
            bar_set.doubleClicked.connect(lambda i: self._drill(i, 3))
        rolling = line_series(
            self.tr("12 months"), [clip(m.rolling_rate) for m in data.months], accent_color()
        )
        add_line_tooltips(rolling, labels, unit="%")
        x = month_axis(months)
        y = QValueAxis()
        values = [clip(m.rate) for m in data.months] + [clip(m.rolling_rate) for m in data.months]
        # steps of 25 % (values are clipped to ±limit)
        low = min(-25, math.floor(min(values) / 25) * 25)
        high = max(25, math.ceil(max(values) / 25) * 25)
        y.setRange(low, high)
        step = 25 if (high - low) / 25 <= 8 else 50
        y.setTickCount(int((high - low) / step) + 1)
        y.setLabelFormat("%.0f %%")
        attach(chart, bars, x, y)
        attach(chart, rolling, x, y)
        set_chart(self.view, chart)

        rows = [
            [
                month_label(m.month),
                m.income_cents,
                m.expense_cents,
                m.saved_cents,
                _percent(m.rate),
                _percent(m.rolling_rate),
                m.one_off_net_cents if m.one_off_income_cents or m.one_off_expense_cents else "",
            ]
            for m in data.months
        ]
        one_off_income, one_off_expenses = data.one_off_income_cents, data.one_off_expense_cents
        rows.append(
            [
                self.tr("Period"),
                data.income_cents,
                data.income_cents - data.saved_cents,
                data.saved_cents,
                _percent(data.rate),
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
        if months[-1] == f"{date.today():%Y-%m}":
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


class CashFlowReport(ReportPage):
    """Sankey diagram: income category -> income group -> expense group -> category."""

    def __init__(self, app: App, parent=None) -> None:
        super().__init__(app, parent)
        self.data: cash_flow.CashFlow | None = None
        self._filter: tuple[date, date, int | None, bool] | None = None
        self.chart = SankeyChart(self.tr("Cash flow"))
        self.chart.node_activated.connect(self._drill)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(self.chart)
        self.note = note_label()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, PAGE_TOP_MARGIN, 0, 0)
        layout.addWidget(scroll, 1)
        layout.addWidget(self.note)

    def refresh(
        self, start: date, end: date, account_id: int | None, one_offs: bool = True
    ) -> None:
        with self.app.session() as s:
            data = cash_flow.cash_flow(s, start, end, account_id, one_offs)
            shifted = reports.shifted_categories(s)
            colors = group_colors(s)
        self.data = data
        self._filter = (start, end, account_id, one_offs)
        total = data.total_cents
        labels = [self._label(n) for n in data.nodes]
        nodes = []
        for n, label in zip(data.nodes, labels, strict=True):
            share = _percent(n.cents / total if total else None)
            nodes.append(
                SankeyNode(
                    n.column,
                    label,
                    _whole_euros(n.cents),
                    n.cents,
                    self._color(n, colors),
                    f"{label}\n{money(n.cents)} · {share}\n{drill_tip()}",
                )
            )
        links = [
            SankeyLink(
                k.source,
                k.target,
                k.cents,
                f"{labels[k.source]} → {labels[k.target]}\n{money(k.cents)}",
            )
            for k in data.links
        ]
        self.chart.set_data(nodes, links, self.tr("No income or expenses in this period."))
        if not data.nodes:
            self.note.setText("")
            return

        balance = data.balance_cents
        notes = [
            self.tr("Income %1, expenses %2, %3 %4.")
            .replace("%1", money(data.income_cents))
            .replace("%2", money(data.expense_cents))
            .replace("%3", self.tr("wealth building") if balance >= 0 else self.tr("deficit"))
            .replace("%4", money(abs(balance))),
            self.tr(
                "Transfers between own accounts are not counted. Categories under 1 % of the "
                "total are combined per group."
            ),
        ]
        if data.income_groups > 1:
            notes.append(
                self.tr(
                    "Which income paid for which expenses is not known: the flows between "
                    "the groups are split in proportion."
                )
            )
        if data.left_out:
            notes.append(
                self.tr(
                    "Not shown, as their sum runs against the flow (e.g. refunds): %1."
                ).replace("%1", ", ".join(f"{o.name} ({money(o.cents)})" for o in data.left_out))
            )
        notes.append(shift_note(shifted))
        notes.append(one_off_note(one_offs, data.one_off_expense_cents, data.one_off_income_cents))
        self.note.setText(" ".join(n for n in notes if n))

    def _label(self, node: cash_flow.Node) -> str:
        if node.kind == "category":
            return one_off_name(node.name, node.one_off)
        if node.kind == "group":
            return node.name
        if node.kind == "other":
            return self.tr("Other (%1)").replace("%1", node.name)
        return {
            "uncategorized": self.tr("Uncategorized"),
            "surplus": self.tr("Wealth building"),
            "deficit": self.tr("From reserves"),
        }[node.kind]

    @staticmethod
    def _color(node: cash_flow.Node, groups: dict[int, str]) -> QColor:
        """Expense groups and their categories in the colours of the budget's group chips
        (the expense colour without a custom theme)."""
        if node.kind == "surplus":
            return accent_color()
        if node.kind == "deficit":
            return amount_color(-1)
        if node.income:
            return income_color()
        return QColor(groups[node.group_id]) if node.group_id in groups else expense_color()

    def _drill(self, index: int) -> None:
        """The bookings behind a node; the balance node: income and expenses."""
        if self.data is None or self._filter is None:
            return
        start, end, account_id, one_offs = self._filter
        node = self.data.nodes[index]
        balance = node.kind in ("surplus", "deficit")
        measure: drilldown.Measure = "saved" if balance else "income" if node.income else "expenses"
        ids = None if balance else node.category_ids
        title = self._label(node)
        if not balance:
            side = self.tr("Income") if node.income else self.tr("Expenses")
            title = f"{side}: {title}"
        open_breakdown(
            self,
            self.app,
            title,
            breakdown_context(self.app, start, end, account_id, one_offs),
            lambda s: drilldown.report_breakdown(s, measure, start, end, account_id, one_offs, ids),
        )


def _whole_euros(cents: int) -> str:
    return f"{fmt_number(round(cents / 100), 0)} €"


# --- year comparison -------------------------------------------------------------------

SECOND_YEAR_COLOR = "#9e9e9e"  # the earlier year, in grey next to the accent colour


class YearComparisonReport(ReportPage):
    """Expenses per category in two years (January up to the same month)."""

    def __init__(self, app: App, parent=None) -> None:
        super().__init__(app, parent)
        self.data: reports.YearComparison | None = None
        self._account: int | None = None
        self._one_offs = True
        self.first_year = QComboBox()
        self.second_year = QComboBox()
        self.same_period = QCheckBox()  # shown when the second year is in progress
        self.same_period.setChecked(True)
        self.same_period.setToolTip(
            self.tr("Compare both years up to the same month, e.g. while a year is not over.")
        )
        controls = QHBoxLayout()
        controls.addWidget(QLabel(self.tr("Compare")))
        controls.addWidget(self.first_year)
        controls.addWidget(QLabel(self.tr("with")))
        controls.addWidget(self.second_year)
        controls.addWidget(self.same_period)
        controls.addStretch(1)
        self.view = chart_view(new_chart(self.tr("Largest categories")))
        self.table = report_table(
            [self.tr("Category"), "", "", self.tr("Difference"), self.tr("Change")], {1, 2, 3, 4}
        )
        self.table.setToolTip(drill_tip())
        self.table.cellDoubleClicked.connect(self._drill)
        self._upto = 12
        self.note = note_label()
        layout = chart_and_table([self.view], self.table, self.note)
        layout.insertLayout(0, controls)
        self.setLayout(layout)
        for w in (self.first_year, self.second_year):
            w.currentIndexChanged.connect(self._redraw)
        self.same_period.toggled.connect(self._redraw)

    def refresh(
        self, start: date, end: date, account_id: int | None, one_offs: bool = True
    ) -> None:
        """Own years; the account filter applies, the period of the bar does not."""
        self._account = account_id
        self._one_offs = one_offs
        with self.app.session() as s:
            first, last = reports.booking_date_range(s)
        years = list(range(first.year, last.year + 1)) if first else [date.today().year]
        widgets = (self.first_year, self.second_year, self.same_period)
        for w in widgets:
            w.blockSignals(True)
        try:
            for combo, default in ((self.first_year, -2), (self.second_year, -1)):
                previous = combo.currentData()
                combo.clear()
                for y in years:
                    combo.addItem(str(y), y)
                choice = previous if previous in years else years[max(default, -len(years))]
                combo.setCurrentIndex(years.index(choice))
        finally:
            for w in widgets:
                w.blockSignals(False)
        self._redraw()

    def _last_month(self) -> int:
        """Month up to which a year in progress has data."""
        return date.today().month if self.second_year.currentData() == date.today().year else 12

    def _redraw(self) -> None:
        first, second = self.first_year.currentData(), self.second_year.currentData()
        if first is None or second is None:
            return
        last_month = self._last_month()
        month_name = QLocale().standaloneMonthName(last_month, QLocale.FormatType.ShortFormat)
        self.same_period.setText(self.tr("Only January – %1").replace("%1", month_name))
        self.same_period.setVisible(last_month < 12)
        upto = last_month if self.same_period.isChecked() else 12
        self._upto = upto
        with self.app.session() as s:
            data = reports.year_comparison(s, first, second, upto, self._account, self._one_offs)
            worth = networth.net_worth(s, f"{date.today():%Y-%m}", self._account)
            shifted = reports.shifted_categories(s)
        self.data = data
        self._draw_chart(data)

        def label(row: reports.YearRow) -> str:
            if row.key == reports.UNCATEGORIZED_KEY:
                return self.tr("Uncategorized")
            return one_off_name(f"{row.group_name}: {row.category_name}", row.one_off)

        self.table.setHorizontalHeaderLabels(
            [self.tr("Category"), str(first), str(second), self.tr("Difference"), self.tr("Change")]
        )
        rows = [
            [label(r), r.first, r.second, r.difference, _percent(r.change, 0)] for r in data.rows
        ]
        spent = data.expenses
        income = data.income
        rows.append(
            [
                self.tr("Income"),
                income[0],
                income[1],
                income[1] - income[0],
                _percent((income[1] - income[0]) / income[0] if income[0] > 0 else None, 0),
            ]
        )
        rows.append(
            [
                self.tr("Expenses"),
                spent[0],
                spent[1],
                spent[1] - spent[0],
                _percent((spent[1] - spent[0]) / spent[0] if spent[0] > 0 else None, 0),
            ]
        )
        fill_table(self.table, rows, bold_last=True, costs=frozenset({3}))
        notes = [
            self.tr("January – %1 of each year.").replace("%1", QLocale().standaloneMonthName(upto))
            if upto < 12
            else self.tr("Whole years.")
        ]
        complete = worth.complete_from if worth.months else None
        if complete and complete > f"{min(first, second)}-01":
            notes.append(
                self.tr(
                    "Not all accounts have data before %1, so earlier months show less."
                ).replace("%1", month_label(complete))
            )
        notes.append(self.tr("The account filter applies, the period above does not."))
        notes.append(shift_note(shifted))
        for i, year in enumerate((first, second)):
            note = one_off_note(self._one_offs, data.one_off_expenses[i], data.one_off_income[i])
            if note:
                notes.append(f"{year}: {note}")
        self.note.setText(" ".join(n for n in notes if n))

    def _drill(self, row: int, column: int) -> None:
        """A category (or income / expenses) in the first (column 1) or second year."""
        if self.data is None or column not in (1, 2):
            return
        year = self.data.first_year if column == 1 else self.data.second_year
        rows = self.data.rows
        if row < len(rows):
            r = rows[row]
            self._drill_year(year, "expenses", r)
        elif row == len(rows):
            self._drill_year(year, "income")
        else:
            self._drill_year(year, "expenses")

    def _drill_year(
        self, year: int, measure: drilldown.Measure, row: reports.YearRow | None = None
    ) -> None:
        first = date(year, 1, 1)
        last = budget.month_last_day(f"{year}-{self._upto:02d}")
        if row is None:
            name = self.tr("Income") if measure == "income" else self.tr("Expenses")
            ids = None
        elif row.key == reports.UNCATEGORIZED_KEY:
            name, ids = self.tr("Uncategorized"), [None]
        else:
            name = one_off_name(f"{row.group_name}: {row.category_name}", row.one_off)
            ids = [row.category_id]
        account_id, one_offs = self._account, self._one_offs
        open_breakdown(
            self,
            self.app,
            f"{name} · {year}",
            breakdown_context(self.app, first, last, account_id, one_offs),
            lambda s: drilldown.report_breakdown(
                s, measure, first, last, account_id, one_offs, ids
            ),
        )

    def _draw_chart(self, data: reports.YearComparison) -> None:
        chart = new_chart(self.tr("Largest categories"))
        largest = sorted(data.rows, key=lambda r: max(r.first, r.second), reverse=True)[:10]
        largest.reverse()  # largest at the top of a horizontal bar chart
        names = [r.category_name or self.tr("Uncategorized") for r in largest]
        labels = [
            one_off_name(f"{r.group_name}: {name}" if names.count(name) > 1 else name, r.one_off)
            for r, name in zip(largest, names, strict=True)
        ]
        first = QBarSet(str(data.first_year))
        first.setColor(QColor(SECOND_YEAR_COLOR))
        second = QBarSet(str(data.second_year))
        second.setColor(accent_color())
        for r in largest:
            first.append(max(r.first, 0) / 100)
            second.append(max(r.second, 0) / 100)
        series = QHorizontalBarSeries()
        series.append(first)
        series.append(second)
        add_bar_tooltips(series, labels)
        for bar_set, year in ((first, data.first_year), (second, data.second_year)):
            bar_set.doubleClicked.connect(
                lambda i, y=year: self._drill_year(y, "expenses", largest[i])
            )
        axis = QBarCategoryAxis()
        axis.append(labels)
        values = [r.first / 100 for r in largest] + [r.second / 100 for r in largest]
        chart.addSeries(series)
        chart.addAxis(axis, Qt.AlignmentFlag.AlignLeft)
        x = money_axis(values)
        chart.addAxis(x, Qt.AlignmentFlag.AlignBottom)
        series.attachAxis(axis)
        series.attachAxis(x)
        set_chart(self.view, chart)


# --- fixed costs & subscriptions -------------------------------------------------------


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
            self._center = [self._text(chart, *line) for line in lines]
            self._legend = [
                (
                    self._legend_dot(chart, piece.brush()),
                    self._text(chart, "", 1.0, False),
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

    def _text(self, chart, text: str, size: float, bold: bool) -> QGraphicsSimpleTextItem:
        item = QGraphicsSimpleTextItem(text, chart)
        font = QFont(self.font())
        font.setPointSizeF(font.pointSizeF() * size)
        font.setBold(bold)
        item.setFont(font)
        item.setBrush(chart.titleBrush())
        return item

    def _legend_dot(self, chart, brush: QBrush) -> QGraphicsEllipseItem:
        dot = QGraphicsEllipseItem(0, 0, LEGEND_DOT, LEGEND_DOT, chart)
        dot.setBrush(brush)
        dot.setPen(QPen(brush.color(), 1))  # outlines the hatched dot
        return dot

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

PRINCIPAL_COLOR = "#2e9e5b"


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
        self.setLayout(chart_and_table([self.debt_view, self.view], self.table, self.note))

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

        known = [m for m in data.months if m.debt is not None]
        if known:
            debt = line_series(
                self.tr("Remaining debt"),
                [m.debt / 100 if m.debt is not None else None for m in data.months],
                expense_color(),
            )
            add_line_tooltips(debt, labels)
            attach(
                debt_chart,
                debt,
                month_axis(months),
                money_axis([m.debt / 100 for m in known], tight=True),
            )
        set_chart(self.debt_view, debt_chart)

        parts = [
            (self.tr("Interest"), [m.interest for m in data.months], expense_color()),
            (self.tr("Repayment"), [m.principal for m in data.months], QColor(PRINCIPAL_COLOR)),
            (self.tr("Special repayments"), [m.extra for m in data.months], accent_color()),
        ]
        bars = QStackedBarSeries()
        for name, values, color in parts:
            bar_set = QBarSet(name)
            bar_set.setColor(color)
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
                .replace("%2", _percent(share, 0))
            )
        self.note.setText(" ".join(notes))


# --- depot -----------------------------------------------------------------------------

SECURITY_COLORS = ["#3b7dd8", "#2e9e5b", "#e8a33d", "#8e5bd8", "#d85b8e", "#5bc0d8", "#9e9e9e"]


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
            bar_set = QBarSet(name)
            bar_set.setColor(QColor(SECURITY_COLORS[i % len(SECURITY_COLORS)]))
            for st in data.statements:
                bar_set.append(st.securities.get(name, 0) / 100)
            bars.append(bar_set)
        add_bar_tooltips(bars, labels)
        invested = line_series(
            self.tr("Invested"), [st.invested / 100 for st in data.statements], QColor("#d9534f")
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
                _percent(p.expense_cents / total if total else None),
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
            .replace("%2", _percent(top / total if total else None, 0))
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
        bars = QBarSet(self.tr("Expenses"))
        bars.setColor(expense_color())
        bars.setLabelColor(self.palette().color(QPalette.ColorRole.WindowText))
        for p in ordered:
            bars.append(p.expense_cents / 100)
        series = QHorizontalBarSeries()
        series.append(bars)
        series.setLabelsVisible(True)
        series.setLabelsPosition(QAbstractBarSeries.LabelsPosition.LabelsOutsideEnd)
        series.setLabelsFormat("@value")
        add_bar_tooltips(series, names)
        axis = QBarCategoryAxis()
        axis.append(labels)
        chart.addSeries(series)
        chart.addAxis(axis, Qt.AlignmentFlag.AlignLeft)
        x = money_axis([p.expense_cents / 100 for p in ordered])
        chart.addAxis(x, Qt.AlignmentFlag.AlignBottom)
        series.attachAxis(axis)
        series.attachAxis(x)
        set_chart(self.view, chart)
