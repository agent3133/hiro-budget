"""Cash flow report (Sankey chart)."""

from __future__ import annotations

from datetime import date

from PySide6.QtGui import (
    QColor,
)
from PySide6.QtWidgets import (
    QFrame,
    QScrollArea,
    QVBoxLayout,
)

from hiro_budget.app import App
from hiro_budget.gui.charts import (
    expense_color,
    income_color,
    one_off_name,
)
from hiro_budget.gui.common import (
    accent_color,
    amount_color,
    fmt_number,
    group_colors,
    money,
)
from hiro_budget.gui.reports.base import (
    PAGE_TOP_MARGIN,
    ReportPage,
    breakdown_context,
    drill_tip,
    fmt_percent,
    note_label,
    one_off_note,
    open_breakdown,
    shift_note,
)
from hiro_budget.gui.sankey import SankeyChart, SankeyLink, SankeyNode
from hiro_budget.services import (
    cash_flow,
    drilldown,
    reports,
)


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
            share = fmt_percent(n.cents / total if total else None)
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
