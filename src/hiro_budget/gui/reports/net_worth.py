"""Net worth report."""

from __future__ import annotations

from datetime import date

from hiro_budget.app import App
from hiro_budget.gui.charts import (
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
    report_table,
    set_chart,
)
from hiro_budget.gui.common import (
    accent_color,
    money,
)
from hiro_budget.gui.reports.base import ReportPage, chart_and_table, note_label
from hiro_budget.services import (
    networth,
)


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
