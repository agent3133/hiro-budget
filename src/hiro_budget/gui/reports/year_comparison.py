"""Year comparison report."""

from __future__ import annotations

from datetime import date

from PySide6.QtCharts import (
    QBarCategoryAxis,
    QHorizontalBarSeries,
)
from PySide6.QtCore import QDate, QLocale, Qt
from PySide6.QtGui import (
    QColor,
)
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
)

from hiro_budget.app import App
from hiro_budget.gui import theme
from hiro_budget.gui.charts import (
    add_bar_tooltips,
    add_bottom_money_axis,
    chart_view,
    fill_table,
    money_axis,
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
    breakdown_context,
    chart_and_table,
    drill_tip,
    fmt_percent,
    note_label,
    one_off_note,
    open_breakdown,
    shift_note,
)
from hiro_budget.services import (
    drilldown,
    networth,
    reports,
)


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
            self.tr(
                "Compare both years up to the same day, the last one with data, e.g. while "
                "a year is not over."
            )
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
        self._cut: date | None = None  # both years end at this day (of the second year)
        self._data_end = date.today()  # the last day with data (latest booking, at most today)
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
        self._data_end = min(last, date.today()) if last else date.today()
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

    def _data_cut(self) -> date | None:
        """The last day with data if the second year is not over by then (#33: the year
        in progress ends at its last booking, not at the end of a month)."""
        end = self._data_end
        if self.second_year.currentData() != end.year or (end.month, end.day) == (12, 31):
            return None
        return end

    def _day(self, day: date) -> str:
        """E.g. "22. Oktober" / "22 October"."""
        return QLocale().toString(
            QDate(day.year, day.month, day.day), self.tr("d MMMM", "date format: day and month")
        )

    def _year_day(self, year: int, cut: date) -> date:
        """The cut day in another year (29 February: 28 in a common year)."""
        end = reports.month_end(date(year, cut.month, 1))
        return end.replace(day=min(cut.day, end.day))

    def _redraw(self) -> None:
        first, second = self.first_year.currentData(), self.second_year.currentData()
        if first is None or second is None:
            return
        cut = self._data_cut()
        if cut is not None:
            self.same_period.setText(
                self.tr("Only %1 – %2")
                .replace("%1", self._day(date(cut.year, 1, 1)))
                .replace("%2", self._day(cut))
            )
        self.same_period.setVisible(cut is not None)
        cut = cut if self.same_period.isChecked() else None
        self._cut = cut
        with self.app.session() as s:
            data = reports.year_comparison(
                s,
                first,
                second,
                cut.month if cut else 12,
                self._account,
                self._one_offs,
                last_day=cut.day if cut else None,
            )
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
            [label(r), r.first, r.second, r.difference, fmt_percent(r.change, 0)] for r in data.rows
        ]
        spent = data.expenses
        income = data.income
        rows.append(
            [
                self.tr("Income"),
                income[0],
                income[1],
                income[1] - income[0],
                fmt_percent((income[1] - income[0]) / income[0] if income[0] > 0 else None, 0),
            ]
        )
        rows.append(
            [
                self.tr("Expenses"),
                spent[0],
                spent[1],
                spent[1] - spent[0],
                fmt_percent((spent[1] - spent[0]) / spent[0] if spent[0] > 0 else None, 0),
            ]
        )
        fill_table(self.table, rows, bold_last=True, costs=frozenset({3}))
        notes = [
            self.tr("%1 – %2 of each year.")
            .replace("%1", self._day(date(second, 1, 1)))
            .replace("%2", self._day(cut))
            if cut
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
        last = self._year_day(year, self._cut) if self._cut else date(year, 12, 31)
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
        # grey beside the accent
        first = new_bar_set(str(data.first_year), QColor(theme.color("second_year")))
        second = new_bar_set(str(data.second_year), accent_color())
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
        add_bottom_money_axis(chart, x)
        series.attachAxis(axis)
        series.attachAxis(x)
        set_chart(self.view, chart)


# --- fixed costs & subscriptions -------------------------------------------------------
