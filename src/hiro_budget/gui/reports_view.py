"""Reports tab with QtCharts: monthly income/expenses, spending by category, trend."""

from __future__ import annotations

from datetime import date

from PySide6.QtCharts import (
    QBarCategoryAxis,
    QBarSeries,
    QBarSet,
    QPieSeries,
    QPieSlice,
    QStackedBarSeries,
)
from PySide6.QtCore import QLocale, QMargins, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QCursor, QFontMetrics, QPalette, QPen
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDateEdit,
    QGraphicsEllipseItem,
    QGraphicsSimpleTextItem,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QTabWidget,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from hiro_budget.app import App
from hiro_budget.gui import shortcuts
from hiro_budget.gui.charts import (
    add_bar_tooltips,
    attach,
    chart_view,
    expense_color,
    income_color,
    legend_dot,
    mark_partial,
    money_axis,
    month_axis,
    month_label,
    month_labels,
    new_bar_set,
    new_chart,
    one_off_name,
    set_chart,
    strip_markup,
    text_label,
)
from hiro_budget.gui.common import (
    accent_color,
    category_choices,
    fmt_number,
    from_qdate,
    group_colors,
    is_dark,
    money,
    to_qdate,
)
from hiro_budget.gui.date_range import DateRangeButton
from hiro_budget.gui.reports import (
    BudgetActualReport,
    CashFlowReport,
    DepotReport,
    MortgageReport,
    NetWorthReport,
    RecurringReport,
    ReportPage,
    SavingsRateReport,
    TopPayeesReport,
    YearComparisonReport,
)
from hiro_budget.gui.reports.base import (
    PAGE_TOP_MARGIN,
    breakdown_context,
    month_range,
    one_off_note,
    open_breakdown,
)
from hiro_budget.gui.reports.donut import (
    DONUT_HOLE,
    DONUT_LABEL_SHARE,
    DONUT_MAX_FIXED,
    DONUT_SIZE,
    LEGEND_DOT,
    donut_ring,
    shade,
)
from hiro_budget.gui.widgets import page_title
from hiro_budget.services import drilldown, lookups, recurring, reports

CATEGORY_LEGEND_MAX = 170  # px for a legend line of the category donut
DONUT_MAX_GROUPS = 10  # group slices; with more groups the smallest are "Others"
CATEGORY_LEGEND_GAP = 3  # px between its lines


class ReportsView(QWidget):
    #: open a booking on the bookings page (from a breakdown of any report)
    show_booking = Signal(int)

    def __init__(self, app: App, parent=None) -> None:
        super().__init__(parent)
        self.app = app
        self.breakdown_dialog = None  # the last one opened from the overview tab

        self.date_from = QDateEdit(calendarPopup=True, displayFormat="dd.MM.yyyy")
        self.date_to = QDateEdit(calendarPopup=True, displayFormat="dd.MM.yyyy")
        self.period = DateRangeButton(self.date_from, self.date_to, self._full_range)
        self.setFocusProxy(self.period)  # keyboard focus when the page is shown
        self.account = QComboBox()
        self.trend_category = QComboBox()
        self.trend_category.setMinimumWidth(220)
        self.totals = QLabel()

        self.account.setToolTip(self.tr("Account"))
        self.one_offs = QCheckBox(self.tr("Include one-offs"))
        self.one_offs.setChecked(bool(app.ui_state.get("reports.one_offs", True)))
        self.one_offs.setToolTip(
            self.tr(
                "Categories marked as one-off on the page Categories & rules (e.g. bonus, tax "
                "refund, repairs). Leave them out to see the regular household per month; "
                "include them for the complete result of a year."
            )
        )
        bar = QHBoxLayout()
        bar.addWidget(self.period)
        bar.addWidget(self.account)
        bar.addWidget(self.one_offs)
        bar.addStretch(1)
        bar.addWidget(self.totals)

        self.monthly_view = chart_view(new_chart(self.tr("Income and expenses per month")))
        self.category_view = chart_view(new_chart(self.tr("Expenses by group")))
        # the donut shows the groups; a click on one shows its categories, this goes back
        self.donut_back = QLabel(
            "<a href='#'>" + self.tr("← All groups") + "</a>", self.category_view
        )
        self.donut_back.linkActivated.connect(lambda _link: self.show_donut_group(None))
        self.donut_back.move(12, 8)
        self.donut_back.hide()
        self.trend_view = chart_view(new_chart(self.tr("Category trend")))
        self.costs_view = chart_view(new_chart(self.tr("Fixed and variable costs per month")))

        # The category of the trend chart is chosen above it, in a row of its own over both
        # columns, so that the charts start at the same height.
        controls = QHBoxLayout()
        controls.addWidget(QLabel(self.tr("Category")))
        controls.addWidget(self.trend_category)
        controls.addStretch(1)

        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.addLayout(controls, 0, 1)
        grid.addWidget(self.monthly_view, 1, 0)
        grid.addWidget(self.trend_view, 1, 1)
        # the costs under the income and expenses: the same months one above the other
        grid.addWidget(self.costs_view, 2, 0)
        grid.addWidget(self.category_view, 2, 1)
        grid.setColumnStretch(0, 3)
        grid.setColumnStretch(1, 2)
        grid.setRowStretch(1, 1)
        grid.setRowStretch(2, 1)
        overview = QWidget()
        overview_layout = QVBoxLayout(overview)
        overview_layout.setContentsMargins(0, PAGE_TOP_MARGIN, 0, 0)
        overview_layout.addLayout(grid, 1)

        # One tab per report; all follow the period and account of the bar above.
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.tabBar().setDrawBase(False)  # no line above the tabs
        self.tabs.addTab(overview, self.tr("Overview"))
        reports_and_titles = [
            (NetWorthReport(app), self.tr("Net worth")),
            (BudgetActualReport(app), self.tr("Budget vs. actual")),
            (SavingsRateReport(app), self.tr("Savings rate")),
            (CashFlowReport(app), self.tr("Cash flow")),
            (YearComparisonReport(app), self.tr("Year comparison")),
            (RecurringReport(app), self.tr("Fixed costs")),
            (MortgageReport(app), self.tr("Mortgage")),
            (DepotReport(app), self.tr("Depot")),
            (TopPayeesReport(app), self.tr("Payees")),
        ]
        self.pages: list[ReportPage] = [page for page, _ in reports_and_titles]
        for page, title in reports_and_titles:
            self.tabs.addTab(page, title)
            page.show_booking.connect(self.show_booking)
        self._stale: set[int] = set()  # pages to redraw when they are shown
        self.tabs.currentChanged.connect(self._tab_changed)
        # Ctrl+PgDn / PgUp: the report tabs (Ctrl+Tab stays the sidebar's pages, #67)
        here = Qt.ShortcutContext.WidgetWithChildrenShortcut
        for index, step in ((0, 1), (1, -1)):
            shortcuts.bind("report_tab", self, lambda s=step: self.step_tab(s), here, index=index)
        for index, step in ((0, -1), (1, 1)):
            shortcuts.bind(
                "report_period", self, lambda s=step: self.period.step(s), here, index=index
            )
        for i in range(9):
            shortcuts.bind(
                "report_n", self, lambda i=i: self.tabs.setCurrentIndex(i), here, index=i
            )

        layout = QVBoxLayout(self)
        layout.addWidget(page_title(self.tr("Reports")))
        layout.addLayout(bar)
        layout.addWidget(self.tabs, 1)

        self.date_from.dateChanged.connect(self.refresh)
        self.date_to.dateChanged.connect(self.refresh)
        self.account.currentIndexChanged.connect(self.refresh)
        self.one_offs.toggled.connect(self._one_offs_toggled)
        self.trend_category.currentIndexChanged.connect(self._refresh_trend)
        self._initialized = False
        self._cats: list[reports.CategoryTotal] = []
        # the donut: its slices (name, categories), centre text and legend
        self._slices: list[tuple[str, list[reports.CategoryTotal]]] = []
        self._donut_group: int | None = None  # the group whose categories are shown
        self._center: list[QGraphicsSimpleTextItem] = []
        self._legend: list[tuple[QGraphicsEllipseItem, QGraphicsSimpleTextItem]] = []
        self.months: list[reports.MonthTotals] = []
        self.partial: tuple[str, date] | None = None  # see reports.partial_month
        self.costs: dict[str, recurring.MonthCosts] = {}
        self._loaded = False  # the data is loaded when the page is first shown
        self._overview_stale = False  # its charts are drawn when the tab is shown (#60)

    # -- data ------------------------------------------------------------------

    def _full_range(self) -> tuple[date, date] | None:
        """First to last booking (the preset "everything")."""
        with self.app.session() as s:
            first, last = reports.booking_date_range(s)
        return (first, last) if first and last else None

    def showEvent(self, event) -> None:
        if not self._loaded:
            self.reload()
        super().showEvent(event)

    def reload(self) -> None:
        """Reload filter options (after import/config changes) and redraw; before the page
        was shown that waits for it (it is not needed at the start of the app)."""
        if not self._loaded and not self.isVisible():
            return
        self._loaded = True
        widgets = (self.date_from, self.date_to, self.account, self.trend_category)
        for w in widgets:
            w.blockSignals(True)
        with self.app.session() as s:
            first, last = reports.booking_date_range(s)
            prev_acc = self.account.currentData()
            self.account.clear()
            self.account.addItem(self.tr("All accounts"), None)
            for a in lookups.accounts(s):
                self.account.addItem(a.name, a.id)
            if prev_acc is not None:
                self.account.setCurrentIndex(max(0, self.account.findData(prev_acc)))

            prev_cat = self.trend_category.currentData()
            self.trend_category.clear()
            for ch in category_choices(s, include_transfers=False):
                self.trend_category.addItem(ch.label, ch.category_id)
                if ch.is_group_header:
                    self.trend_category.model().item(self.trend_category.count() - 1).setEnabled(
                        False
                    )
            idx = self.trend_category.findData(prev_cat) if prev_cat else -1
            if idx < 0:  # default: first real category
                idx = next(
                    (
                        i
                        for i in range(self.trend_category.count())
                        if self.trend_category.itemData(i) is not None
                    ),
                    -1,
                )
            self.trend_category.setCurrentIndex(idx)

        if first is not None and not self._initialized:
            # Default range: the last 12 months with data.
            if last.month == 12:
                start = date(last.year, 1, 1)
            else:
                start = date(last.year - 1, last.month + 1, 1)
            self.date_from.setDate(to_qdate(max(first, start)))
            self.date_to.setDate(to_qdate(last))
            self._initialized = True
        elif first is not None:
            if to_qdate(last) > self.date_to.date():
                self.date_to.setDate(to_qdate(last))
        for w in widgets:
            w.blockSignals(False)
        self.period.refresh()  # the dates were set quietly
        self.refresh()

    def _filter(self) -> tuple[date, date, int | None]:
        return (
            from_qdate(self.date_from.date()),
            from_qdate(self.date_to.date()),
            (self.account.currentData()),
        )

    def _one_offs_toggled(self, on: bool) -> None:
        self.app.ui_state.set("reports.one_offs", on)
        self.refresh()

    def refresh(self) -> None:
        if not self._loaded:  # e.g. the export before the page was shown
            self._loaded = True
            self.reload()
            return
        start, end, account = self._filter()
        one_offs = self.one_offs.isChecked()
        with self.app.session() as s:
            months = reports.monthly_totals(s, start, end, account, one_offs)
        self.months = months  # the totals in the bar above every tab
        income = sum(m.income_cents for m in months)
        expenses = sum(m.expense_cents for m in months)
        text = (
            self.tr("Income %1 · Expenses %2 · Balance %3")
            .replace("%1", money(income))
            .replace("%2", money(expenses))
            .replace("%3", money(income - expenses))
        )
        note = one_off_note(
            one_offs,
            sum(m.one_off_expense_cents for m in months),
            sum(m.one_off_income_cents for m in months),
        )
        if note:
            text += " · " + (self.tr("with one-offs") if one_offs else self.tr("without one-offs"))
        self.totals.setText(text)
        self.totals.setToolTip(note)
        # Redraw the visible report now, the others (the overview too) when they are shown.
        self._overview_stale = True
        self._stale = set(range(len(self.pages)))
        self._tab_changed()

    def ensure_overview(self) -> None:
        """The overview's data and charts for the current filter (when its tab is shown,
        and before an export, which takes its category totals)."""
        if not self._overview_stale:
            return
        self._overview_stale = False
        start, end, account = self._filter()
        one_offs = self.one_offs.isChecked()
        with self.app.session() as s:
            cats = reports.category_totals(s, start, end, account, one_offs)
            self.costs = recurring.costs_by_month(s, start, end, account, one_offs)
            self.partial = reports.partial_month(s, end)  # a month with data missing
        self._draw_monthly(self.months)
        self._draw_costs()
        self._draw_categories(cats)
        self._refresh_trend()

    def nav_state(self) -> int:
        """The report shown, for going back here (history)."""
        return self.tabs.currentIndex()

    def restore_nav_state(self, index: int) -> None:
        self.tabs.setCurrentIndex(index)

    def focus_regions(self) -> list[QWidget]:
        """F6: the filter bar, then the report tabs."""
        return [self.period, self.tabs.tabBar()]

    def step_tab(self, step: int) -> None:
        """The next / previous report tab, round."""
        self.tabs.setCurrentIndex((self.tabs.currentIndex() + step) % self.tabs.count())

    def _tab_changed(self) -> None:
        page = self.tabs.currentWidget()
        if self.tabs.currentIndex() == 0:
            self.ensure_overview()
        if isinstance(page, ReportPage) and self.pages.index(page) in self._stale:
            self._stale.discard(self.pages.index(page))
            page.refresh(*self._filter(), one_offs=self.one_offs.isChecked())

    # -- charts ----------------------------------------------------------------

    def _draw_monthly(self, months: list[reports.MonthTotals]) -> None:
        chart = new_chart(self.tr("Income and expenses per month"))
        income = new_bar_set(self.tr("Income"), income_color())
        expenses = new_bar_set(self.tr("Expenses"), expense_color())
        # Balance as a third bar (a line would be invisible with a single month).
        net = new_bar_set(self.tr("Balance"), accent_color())
        for m in months:
            income.append(m.income_cents / 100)
            expenses.append(m.expense_cents / 100)
            net.append(m.net_cents / 100)
        series = QBarSeries()
        for bar_set, measure in ((income, "income"), (expenses, "expenses"), (net, "saved")):
            series.append(bar_set)
            bar_set.doubleClicked.connect(
                lambda i, m=measure: self._drill_month(months[i].month, m)
            )
        month_keys = [m.month for m in months]
        mark_partial([income, expenses, net], month_keys, self.partial)
        axis_labels = month_labels(month_keys, self.partial)
        add_bar_tooltips(series, [strip_markup(label) for label in axis_labels])
        chart.addSeries(series)

        x = QBarCategoryAxis()
        x.append(axis_labels)
        values = [m.income_cents / 100 for m in months] + [m.expense_cents / 100 for m in months]
        values += [m.net_cents / 100 for m in months]
        attach(chart, series, x, money_axis(values))
        set_chart(self.monthly_view, chart)

    def _draw_costs(self) -> None:
        """Each month's expenses as stacked bars: fixed costs below, variable above."""
        chart = new_chart(self.tr("Fixed and variable costs per month"))
        months = [m.month for m in self.months]
        if not months:
            set_chart(self.costs_view, chart)
            return
        costs = [self.costs.get(m, recurring.MonthCosts(m)) for m in months]
        fixed = new_bar_set(self.tr("Fixed costs"), accent_color())
        variable = QBarSet(self.tr("Variable costs"))
        neutral = self.palette().color(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text)
        variable.setBrush(QBrush(neutral, Qt.BrushStyle.BDiagPattern))  # as on Fixed costs
        variable.setPen(QPen(neutral, 1))
        for c in costs:
            fixed.append(c.fixed_cents / 100)
            variable.append(c.variable_cents / 100)
        bars = QStackedBarSeries()
        bars.append(fixed)
        bars.append(variable)
        mark_partial([fixed], months, self.partial)  # the hatched set keeps its pattern
        labels = [strip_markup(label) for label in month_labels(months, self.partial)]

        def hovered(status: bool, index: int, _bar_set: QBarSet) -> None:
            if status:
                c = costs[index]
                share = c.fixed_cents * 100 / c.expense_cents if c.expense_cents > 0 else 0
                QToolTip.showText(
                    QCursor.pos(),
                    self.tr("%1: fixed %2 (%3 %), variable %4, expenses %5")
                    .replace("%1", labels[index])
                    .replace("%2", money(c.fixed_cents))
                    .replace("%3", fmt_number(share, 0))
                    .replace("%4", money(c.variable_cents))
                    .replace("%5", money(c.expense_cents)),
                )
            else:
                QToolTip.hideText()

        bars.hovered.connect(hovered)
        for bar_set in (fixed, variable):
            bar_set.doubleClicked.connect(lambda i: self._drill_month(months[i], "expenses"))
        totals = [v for c in costs for v in (c.expense_cents / 100, c.variable_cents / 100)]
        attach(chart, bars, month_axis(months, self.partial), money_axis(totals))
        set_chart(self.costs_view, chart)

    def _draw_categories(self, cats: list[reports.CategoryTotal]) -> None:
        """New data for the donut; a group shown stays shown while it has expenses."""
        self._cats = cats
        shown = {c.group_id for c in cats if c.expense_cents > 0 and not _uncategorized(c)}
        if self._donut_group not in shown:
            self._donut_group = None
        self._draw_donut()

    def show_donut_group(self, group_id: int | None) -> None:
        """The categories of a group in the donut, or (None) all groups."""
        self._donut_group = group_id
        self._draw_donut()

    def _draw_donut(self) -> None:
        """The expenses as a donut (as on Fixed costs), the legend on its right: by budget
        group in the group colours (#52: "Others" of categories was half of all expenses),
        or the categories of one group in shades of its colour (the largest, then "Others").
        A click on a group shows its categories; a double-click on a category its bookings."""
        group = self._donut_group
        positive = [c for c in self._cats if c.expense_cents > 0]
        if group is not None:
            positive = [c for c in positive if c.group_id == group and not _uncategorized(c)]
        title = (
            self.tr("Expenses by group")
            if group is None
            else self.tr("Expenses: %1").replace("%1", positive[0].group_name)
        )
        chart = new_chart(title)
        chart.legend().hide()
        self.donut_back.setVisible(group is not None)
        self.donut_back.raise_()
        total = sum(c.expense_cents for c in positive)
        self._slices, self._center, self._legend = [], [], []
        if not total:
            set_chart(self.category_view, chart)
            return
        with self.app.session() as s:
            colors = group_colors(s)
        series = QPieSeries()
        series.setHoleSize(DONUT_HOLE)
        series.setPieSize(DONUT_SIZE)
        series.setPieEndAngle(-360)  # from 12 o'clock counter-clockwise, as on Fixed costs
        ring = donut_ring(chart, self.palette())  # gap between the slices
        ink = chart.titleBrush().color()
        metrics = QFontMetrics(self.font())
        legend_width = 0
        texts = []
        slices = (
            self._group_slices(positive, colors)
            if group is None
            else self._category_slices(positive, colors)
        )
        for name, items, brush in slices:
            cents = sum(c.expense_cents for c in items)
            share = cents * 100 / total
            piece = series.append(f"{fmt_number(share, 0)} %", cents / 100)
            if brush is not None:
                piece.setBrush(brush)
            piece.setBorderColor(ring)
            piece.setBorderWidth(2)
            piece.setLabelVisible(share >= DONUT_LABEL_SHARE)
            piece.setLabelColor(ink)
            piece.setLabelPosition(QPieSlice.LabelPosition.LabelInsideHorizontal)
            piece.setProperty("drill_label", name)  # the breakdown menu (#70)
            lines = [f"{name}: {money(cents)} ({fmt_number(share, 1)} %)"]
            if len(items) > 1:  # a group or "Others": the categories in it
                listed = items[:DONUT_MAX_FIXED]
                lines += [f"{self._category_label(c)}: {money(c.expense_cents)}" for c in listed]
                if len(items) > len(listed):
                    lines.append("…")
            if (
                group is None
                and len({c.group_id for c in items}) == 1
                and not any(_uncategorized(c) for c in items)
            ):
                lines.append(self.tr("Click: the categories of the group"))
                piece.clicked.connect(lambda g=items[0].group_id: self.show_donut_group(g))
            else:
                piece.doubleClicked.connect(
                    lambda items=items, name=name: self._drill_categories(items, name)
                )
            tip = "\n".join(lines)
            piece.hovered.connect(lambda on, p=piece, tip=tip: self._hover(p, on, tip))
            self._slices.append((name, items))
            texts.append(f"{name}  {fmt_number(share, 0)} %")
            legend_width = max(legend_width, metrics.horizontalAdvance(texts[-1]))
        chart.addSeries(series)  # slices without a brush get the chart theme's colours
        self._legend = [
            (legend_dot(chart, piece.brush(), LEGEND_DOT), text_label(chart, self.font(), text))
            for piece, text in zip(series.slices(), texts, strict=True)
        ]
        center_name = self.tr("expenses") if group is None else positive[0].group_name
        self._center = [
            text_label(chart, self.font(), f"{fmt_number(total / 100, 0)} €", 1.2, bold=True),
            text_label(chart, self.font(), center_name),
        ]
        # the legend right of the donut: at most a third of the chart's width per name
        room = min(legend_width, CATEGORY_LEGEND_MAX) + LEGEND_DOT + 6
        chart.setMargins(QMargins(4, 4, room + 16, 4))
        chart.plotAreaChanged.connect(lambda _area: self._place_donut_overlays())
        set_chart(self.category_view, chart)
        self._place_donut_overlays()

    def _group_slices(
        self, positive: list[reports.CategoryTotal], colors: dict[int, str]
    ) -> list[tuple[str, list[reports.CategoryTotal], QBrush | None]]:
        """(name, categories, brush) per budget group, largest first, in the group colours
        (a colour used again gets the next shade, so groups differ); with more than
        ``DONUT_MAX_GROUPS`` the smallest are "Others"; uncategorized last."""
        per_group: dict[int | None, list[reports.CategoryTotal]] = {}
        for c in positive:
            per_group.setdefault(None if _uncategorized(c) else c.group_id, []).append(c)
        uncategorized = per_group.pop(None, [])
        ranked = sorted(per_group.values(), key=lambda cs: -sum(c.expense_cents for c in cs))
        keep = ranked if len(ranked) <= DONUT_MAX_GROUPS else ranked[: DONUT_MAX_GROUPS - 1]
        rest = [c for cs in ranked[len(keep) :] for c in cs]
        neutral = self.palette().color(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text)
        used: dict[str, int] = {}
        out = []
        for cs in keep:
            group_id = cs[0].group_id
            brush = None
            if group_id in colors:
                n = used.get(colors[group_id], 0)
                used[colors[group_id]] = n + 1
                brush = QBrush(shade(QColor(colors[group_id]), n))
            out.append((cs[0].group_name, cs, brush))
        if rest:
            other = neutral.lighter(130) if is_dark() else neutral.darker(115)
            groups = len({c.group_id for c in rest})
            out.append((self.tr("Others (%1)").replace("%1", str(groups)), rest, QBrush(other)))
        if uncategorized:
            out.append((self.tr("Uncategorized"), uncategorized, QBrush(neutral)))
        return out

    def _category_label(self, c: reports.CategoryTotal) -> str:
        if c.key == reports.UNCATEGORIZED_KEY:
            return self.tr("Uncategorized")
        return one_off_name(c.category_name, c.one_off)

    def _category_slices(
        self, positive: list[reports.CategoryTotal], colors: dict[int, str]
    ) -> list[tuple[str, list[reports.CategoryTotal], QBrush | None]]:
        """(name, categories, brush) per slice: the ``DONUT_MAX_FIXED`` largest categories
        (all when one more fits), ordered by budget group (largest first) in shades of the
        group colour (None: the chart theme's colour), then "Others"."""
        fits = DONUT_MAX_FIXED + 1
        keep = positive if len(positive) <= fits else positive[:DONUT_MAX_FIXED]
        rest = positive[len(keep) :]
        names = [self._category_label(c) for c in keep]
        per_group: dict[int | None, list[int]] = {}
        for i, c in enumerate(keep):
            group = c.group_id if c.key != reports.UNCATEGORIZED_KEY else None
            per_group.setdefault(group, []).append(i)
        order = sorted(
            per_group,
            key=lambda g: (g is None, -sum(keep[i].expense_cents for i in per_group[g])),
        )
        neutral = self.palette().color(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text)
        out = []
        for g in order:
            base = QColor(colors[g]) if g in colors else None
            for n, i in enumerate(per_group[g]):
                c = keep[i]
                name = names[i]
                if names.count(name) > 1 and c.group_name:  # the same name in two groups
                    name = f"{c.group_name}: {name}"
                if base is not None:
                    brush = QBrush(shade(base, n))
                else:  # uncategorized, or a theme without group colours
                    brush = QBrush(neutral) if g is None else None
                out.append((name, [c], brush))
        if rest:
            other = neutral.lighter(130) if is_dark() else neutral.darker(115)
            out.append((self.tr("Others (%1)").replace("%1", str(len(rest))), rest, QBrush(other)))
        return out

    def _hover(self, piece: QPieSlice, on: bool, tip: str) -> None:
        piece.setExploded(on)
        piece.setExplodeDistanceFactor(0.04)
        if on:
            QToolTip.showText(QCursor.pos(), tip)
        else:
            QToolTip.hideText()

    def _place_donut_overlays(self) -> None:
        """The total in the middle of the hole, the legend in one column on the right."""
        chart = self.category_view.chart()
        area = chart.plotArea()
        heights = [item.boundingRect().height() for item in self._center]
        top = area.center().y() - sum(heights) / 2
        for item, height in zip(self._center, heights, strict=True):
            item.setPos(area.center().x() - item.boundingRect().width() / 2, top)
            top += height
        if not self._legend:
            return
        metrics = QFontMetrics(self.font())
        row = metrics.height() + CATEGORY_LEGEND_GAP
        x = area.right() + 8
        y = area.center().y() - len(self._legend) * row / 2
        room = int(chart.size().width() - x - LEGEND_DOT - 6 - 8)
        for (dot, label), (name, _items) in zip(self._legend, self._slices, strict=True):
            share = label.text().rsplit("  ", 1)[1]
            fitted = metrics.elidedText(
                name, Qt.TextElideMode.ElideRight, room - metrics.horizontalAdvance(f"  {share}")
            )
            label.setText(f"{fitted}  {share}")
            dot.setPos(x, y + (metrics.height() - LEGEND_DOT) / 2)
            label.setPos(x + LEGEND_DOT + 6, y)
            y += row

    def _refresh_trend(self) -> None:
        cat_id = self.trend_category.currentData()
        chart = new_chart(self.tr("Category trend"))
        chart.legend().hide()
        if cat_id is None:
            set_chart(self.trend_view, chart)
            return
        start, end, account = self._filter()
        with self.app.session() as s:
            points = reports.category_trend(s, cat_id, start, end, account)
        # Expense categories are shown as positive amounts; refunds can make a month negative.
        sign = -1 if sum(v for _, v in points) < 0 else 1
        values = [sign * v / 100 for _, v in points]
        bars = new_bar_set(
            self.trend_category.currentText().strip(),
            accent_color() if sign > 0 else expense_color(),
        )
        for v in values:
            bars.append(v)
        series = QBarSeries()
        series.append(bars)
        name = self.trend_category.currentText().strip()
        bars.doubleClicked.connect(
            lambda i: self._drill_trend(cat_id, name, points[i][0], sign > 0)
        )
        labels = [month_label(m) for m, _ in points]
        add_bar_tooltips(series, labels)
        chart.addSeries(series)
        x = QBarCategoryAxis()
        # The trend chart is narrow: "11/25" would be cut to "1...", short month names fit
        # (the tooltips keep the year).
        x.append(
            [
                QLocale().standaloneMonthName(int(m[5:]), QLocale.FormatType.ShortFormat)
                for m, _ in points
            ]
            if len(points) > 6
            else labels
        )
        attach(chart, series, x, money_axis(values))
        chart.setTitle(
            self.tr("Trend: %1").replace("%1", self.trend_category.currentText().strip())
        )
        set_chart(self.trend_view, chart)

    # -- drill-down --------------------------------------------------------------

    def _breakdown(self, title: str, first: date, last: date, fetch, one_offs=None) -> None:
        _start, _end, account = self._filter()
        context = breakdown_context(self.app, first, last, account, one_offs)
        open_breakdown(self, self.app, title, context, fetch)

    def _drill_month(self, month: str, measure: drilldown.Measure) -> None:
        start, end, account = self._filter()
        first, last = month_range(month, start, end)
        one_offs = self.one_offs.isChecked()
        names = {
            "income": self.tr("Income"),
            "expenses": self.tr("Expenses"),
            "saved": self.tr("Balance"),
        }
        self._breakdown(
            f"{names[measure]} · {month_label(month)}",
            first,
            last,
            lambda s: drilldown.report_breakdown(s, measure, first, last, account, one_offs),
            one_offs,
        )

    def _drill_categories(self, cats: list[reports.CategoryTotal], name: str = "") -> None:
        """Expenses of one category (or of the categories summed into "Others")."""
        if not cats:
            return
        start, end, account = self._filter()
        one_offs = self.one_offs.isChecked()
        c = cats[0]
        if not name:
            name = (
                self.tr("Uncategorized")
                if c.key == reports.UNCATEGORIZED_KEY
                else one_off_name(f"{c.group_name}: {c.category_name}", c.one_off)
            )
        ids = [c.category_id for c in cats]
        self._breakdown(
            self.tr("Expenses: %1").replace("%1", name),
            start,
            end,
            lambda s: drilldown.report_breakdown(s, "expenses", start, end, account, one_offs, ids),
            one_offs,
        )

    def _drill_trend(self, cat_id: int, name: str, month: str, income: bool) -> None:
        """A month of the category trend (one-offs are always in it)."""
        start, end, account = self._filter()
        first, last = month_range(month, start, end)
        measure: drilldown.Measure = "income" if income else "expenses"
        self._breakdown(
            f"{name} · {month_label(month)}",
            first,
            last,
            lambda s: drilldown.report_breakdown(s, measure, first, last, account, True, [cat_id]),
        )


def _uncategorized(c: reports.CategoryTotal) -> bool:
    return c.key == reports.UNCATEGORIZED_KEY
