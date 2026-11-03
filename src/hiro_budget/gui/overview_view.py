"""Start page: what needs attention now (money to assign, overspending, bookings without
a category, missing statements) and how things are going (net worth, this month, fixed
costs due soon). The key figures lead to their pages with a click."""

from __future__ import annotations

import html
from datetime import date

from PySide6.QtCore import QLocale, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from hiro_budget.app import App
from hiro_budget.gui.charts import (
    add_line_tooltips,
    attach,
    chart_view,
    line_series,
    money_axis,
    month_axis,
    month_label,
    new_chart,
    set_chart,
)
from hiro_budget.gui.common import (
    accent_color,
    amount_color,
    fmt_date,
    money,
    warning_color,
)
from hiro_budget.gui.widgets import DetailCard, StatCard, banner, page_title
from hiro_budget.services import overview
from hiro_budget.services.overview import Overview

RIGHT = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter


class OverviewView(QWidget):
    show_budget = Signal()
    show_uncategorized = Signal()
    show_review = Signal()
    show_net_worth = Signal()
    show_account = Signal(int)  # the bookings of an account (id)

    def __init__(self, app: App, parent=None) -> None:
        super().__init__(parent)
        self.app = app
        self.data: Overview | None = None
        self.stale = True  # computed when shown (showEvent)
        self._day = date.today()

        self.title = page_title(self.tr("Overview"))
        self.today = QLabel()
        self.today.setObjectName("propName")
        title_row = QHBoxLayout()
        title_row.addWidget(self.title)
        title_row.addSpacing(12)
        title_row.addWidget(self.today, 0, Qt.AlignmentFlag.AlignBottom)
        title_row.addStretch(1)

        # -- key figures: each leads to its page --
        self.cards = {
            "assign": StatCard(self.tr("Ready to Assign")),
            "overspent": StatCard(self.tr("Overspent")),
            "uncategorized": StatCard(self.tr("Uncategorized")),
            "net_worth": StatCard(self.tr("Net worth")),
        }
        self.cards["assign"].clicked.connect(self.show_budget)
        self.cards["overspent"].clicked.connect(self.show_budget)
        self.cards["uncategorized"].clicked.connect(self._uncategorized_clicked)
        self.cards["net_worth"].clicked.connect(self.show_net_worth)
        cards = QHBoxLayout()
        cards.setSpacing(12)
        for card in self.cards.values():
            cards.addWidget(card, 1)

        # -- net worth over the last year --
        self.chart = chart_view(new_chart(""))
        self.chart.setMinimumHeight(220)

        # -- this month --
        self.month = DetailCard(self.tr("This month"))
        self.month_values = {key: QLabel() for key in ("income", "expenses", "saved", "rate")}
        form = QFormLayout()
        form.setHorizontalSpacing(24)
        for key, label in (
            ("income", self.tr("Income")),
            ("expenses", self.tr("Expenses")),
            ("saved", self.tr("Saved")),
            ("rate", self.tr("Savings rate")),
        ):
            name = QLabel(label)
            name.setObjectName("propName")
            self.month_values[key].setAlignment(RIGHT)
            form.addRow(name, self.month_values[key])
        self.month.body.addLayout(form)
        self.month.body.addStretch(1)

        # -- fixed costs due soon --
        self.upcoming_card = DetailCard(self.tr("Due soon"))
        self.upcoming_card.set_heading(self.tr("Fixed costs in the next 30 days"))
        self.upcoming = QTreeWidget()
        self.upcoming.setColumnCount(4)
        self.upcoming.setHeaderLabels(
            [self.tr("Due"), self.tr("Payee"), self.tr("Amount"), self.tr("Category")]
        )
        self.upcoming.setRootIsDecorated(False)
        self.upcoming.setUniformRowHeights(True)
        self.upcoming.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.upcoming.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        header = self.upcoming.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for col in (0, 2, 3):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        self.no_upcoming = QLabel(self.tr("Nothing due in the next 30 days."))
        self.no_upcoming.setObjectName("propName")
        self.upcoming_card.body.addWidget(self.upcoming, 1)
        self.upcoming_card.body.addWidget(self.no_upcoming)

        # -- notes: missing statements, suggestions to review --
        self.notes_card = DetailCard(self.tr("Notes"))
        self.notes_card.set_heading(self.tr("Statements and suggestions"))
        self.notes = QVBoxLayout()
        self.notes.setSpacing(6)
        self.notes_card.body.addLayout(self.notes)
        self.notes_card.body.addStretch(1)

        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(12)
        grid.addWidget(self.chart, 0, 0)
        grid.addWidget(self.month, 0, 1)
        grid.addWidget(self.upcoming_card, 1, 0)
        grid.addWidget(self.notes_card, 1, 1)
        grid.setColumnStretch(0, 3)
        grid.setColumnStretch(1, 2)
        grid.setRowStretch(1, 1)

        layout = QVBoxLayout(self)
        layout.addLayout(title_row)
        layout.addLayout(cards)
        layout.addSpacing(4)
        layout.addLayout(grid, 1)

    # -- data ----------------------------------------------------------------------

    def refresh(self) -> None:
        with self.app.session() as s:
            self.data = overview.overview(s, self.app.settings)
        self._fill(self.data)
        self.stale = False
        self._day = date.today()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        # Current when shown: computed again only after bookings, budget or configuration
        # changed (MainWindow sets ``stale``) or on a new day (#61: on every visit before).
        if self.stale or self._day != date.today():
            self.refresh()

    def focus_regions(self) -> list[QWidget]:
        """F6: the key figures (Tab between them, Space / Return opens), the chart."""
        return [self.cards["assign"], self.chart]

    def _uncategorized_clicked(self) -> None:
        if self.data and not self.data.uncategorized and self.data.to_review:
            self.show_review.emit()
        else:
            self.show_uncategorized.emit()

    # -- display -------------------------------------------------------------------

    def _fill(self, o: Overview) -> None:
        today = date.today()
        self.today.setText(QLocale().toString(today, QLocale.FormatType.LongFormat))
        y, m = map(int, o.month.split("-"))
        month_name = f"{QLocale().standaloneMonthName(m)} {y}"

        rta = o.ready_to_assign
        self.cards["assign"].set_value(
            money(rta),
            self.tr("to give to categories in %1").replace("%1", month_name)
            if rta > 0
            else self.tr("assigned too much: take money back")
            if rta < 0
            else self.tr("every euro has a job"),
            "pos" if rta > 0 else "neg" if rta < 0 else "",
        )
        if o.overspent:
            names = ", ".join(name for name, _ in o.overspent[:3])
            more = len(o.overspent) - 3
            if more > 0:
                names += " " + self.tr("and %n more", "", more)
            self.cards["overspent"].set_value(
                self.tr("%n categories", "", len(o.overspent)), names, "neg"
            )
        elif o.underfunded:
            self.cards["overspent"].set_value(
                "0",
                self.tr("%n target(s) not funded yet", "", o.underfunded),
                "warn",
            )
        else:
            self.cards["overspent"].set_value("0", self.tr("all categories covered"), "pos")
        self.cards["uncategorized"].set_value(
            QLocale().toString(o.uncategorized),
            self.tr("%n suggestion(s) to review", "", o.to_review)
            if o.to_review
            else self.tr("bookings without a category")
            if o.uncategorized
            else self.tr("everything categorized"),
            "warn" if o.uncategorized or o.to_review else "pos",
        )
        now, change = o.net_worth_now, o.net_worth_change
        self.cards["net_worth"].set_value(
            money(now) if now is not None else "–",
            (
                self.tr("%1 in 12 months").replace(
                    "%1", ("+" if change > 0 else "") + money(change)
                )
                if change is not None
                else ""
            ),
        )

        self._fill_chart(o)
        self.month.set_heading(month_name)
        values = self.month_values
        values["income"].setText(money(o.income))
        values["expenses"].setText(money(-o.expenses))
        values["saved"].setText(money(o.saved))
        values["saved"].setStyleSheet(f"color: {amount_color(o.saved).name()};")
        rate = o.saved / o.income if o.income > 0 else None
        values["rate"].setText(
            QLocale().toString(rate * 100, "f", 0) + " %" if rate is not None else "–"
        )

        self.upcoming.clear()
        # payments whose day has passed without a booking: a group of their own on top
        # (a statement may be missing), then the ones still to come (#31)
        overdue = [r for r in o.upcoming if r.next_due < today]
        coming = [r for r in o.upcoming if r.next_due >= today]
        if overdue:
            self._upcoming_group(
                self.tr("Overdue – not booked yet (%1)").replace("%1", str(len(overdue))),
                warning_color(),
            )
        for r in overdue + coming:
            if overdue and r is (coming[0] if coming else None):
                self._upcoming_group(self.tr("Due in the next 30 days"))
            item = QTreeWidgetItem(
                [fmt_date(r.next_due), r.payee, money(-r.amount_cents), r.category or ""]
            )
            item.setTextAlignment(2, RIGHT)
            item.setForeground(2, amount_color(-r.amount_cents))
            if r.next_due < today:
                item.setToolTip(
                    0,
                    self.tr(
                        "Expected on %1, not booked yet: paid late, ended, or a statement is "
                        "missing"
                    ).replace("%1", fmt_date(r.next_due)),
                )
                item.setForeground(0, warning_color())
            self.upcoming.addTopLevelItem(item)
        self.upcoming.setVisible(bool(o.upcoming))
        self.no_upcoming.setVisible(not o.upcoming)

        while self.notes.count():  # rebuilt each time
            w = self.notes.takeAt(0).widget()
            if w is not None:
                w.deleteLater()
        for account, gap in o.gaps:
            self.notes.addWidget(
                banner(
                    QLabel(
                        self.tr("%1: statements missing between %2 and %3 (%4).")
                        .replace("%1", account)
                        .replace("%2", fmt_date(gap.after.period_end))
                        .replace("%3", fmt_date(gap.before.period_start))
                        .replace("%4", money(gap.difference_cents))
                    ),
                    "warning",
                )
            )
        for a in o.stale:
            label = QLabel(
                self.tr("%1: no booking since %2 (%3 days). <a href='#'>Show bookings</a>")
                .replace("%1", html.escape(a.name))
                .replace("%2", fmt_date(a.last))
                .replace("%3", str(a.days))
            )
            label.setToolTip(
                self.tr(
                    "A statement may be missing, or the cash was not counted. Turn the "
                    "reminder off for an account under Accounts."
                )
            )
            label.linkActivated.connect(lambda _l, i=a.account_id: self.show_account.emit(i))
            self.notes.addWidget(banner(label, "warning"))
        if o.to_review:
            self.notes.addWidget(
                banner(
                    QLabel(
                        self.tr("%n suggestion(s) of the classifier to review.", "", o.to_review)
                    ),
                    "info",
                )
            )
        if not o.gaps and not o.to_review and not o.stale:
            self.notes.addWidget(
                banner(QLabel(self.tr("All statements connect; nothing to check.")), "success")
            )

    def _upcoming_group(self, title: str, color=None) -> None:
        """A heading row in the list of due payments."""
        item = QTreeWidgetItem([title])
        font = item.font(0)
        font.setBold(True)
        item.setFont(0, font)
        if color is not None:
            item.setForeground(0, color)
        self.upcoming.addTopLevelItem(item)
        item.setFirstColumnSpanned(True)

    def _fill_chart(self, o: Overview) -> None:
        chart = new_chart(self.tr("Net worth"))
        if len(o.net_worth) >= 2:
            months = [m for m, _ in o.net_worth]
            values = [cents / 100 for _, cents in o.net_worth]
            line = line_series(self.tr("Net worth"), values, accent_color())
            add_line_tooltips(line, [month_label(m) for m in months])
            attach(chart, line, month_axis(months), money_axis(values, tight=True))
            chart.legend().hide()
        set_chart(self.chart, chart)
