"""Base class and helpers shared by the report pages."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date

from PySide6.QtCore import QT_TRANSLATE_NOOP, QCoreApplication, QLocale, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from hiro_budget.app import App
from hiro_budget.gui.breakdown_dialog import BreakdownDialog
from hiro_budget.gui.common import (
    fmt_date,
    money,
)
from hiro_budget.services import (
    budget,
    drilldown,
    lookups,
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
            name = lookups.account_name(s, account_id)
        if name is not None:
            parts.append(
                QCoreApplication.translate("report_pages", "Account: %1").replace("%1", name)
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


def fmt_percent(value: float | None, decimals: int = 1) -> str:
    """Share as a percentage in the UI language (71,9 % / 71.9 %)."""
    if value is None:
        return "–"
    return f"{QLocale().toString(value * 100, 'f', decimals)} %"
