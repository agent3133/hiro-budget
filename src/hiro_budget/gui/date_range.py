"""Date range as one drop-down button ("📅 16.01.2021 – 22.10.2026"), as in the design.

The button wraps the two date fields a page already filters with: they move into the
popup, next to presets (this month, last year, ...), so the page keeps reading and
setting them as before.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, timedelta

from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import (
    QDateEdit,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
)

from hiro_budget.gui.common import fmt_date, from_qdate, to_qdate
from hiro_budget.gui.widgets import caption

ICON_CALENDAR = ""  # Calendar (Segoe Fluent Icons / MDL2 Assets)


def _month_start(d: date) -> date:
    return d.replace(day=1)


def _month_end(d: date) -> date:
    return (_month_start(d) + timedelta(days=32)).replace(day=1) - timedelta(days=1)


class DateRangeButton(QPushButton):
    def __init__(
        self,
        date_from: QDateEdit,
        date_to: QDateEdit,
        full_range: Callable[[], tuple[date, date] | None],
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("dateRange")
        self.date_from, self.date_to = date_from, date_to
        self.full_range = full_range  # first and last booking: the preset "everything"
        from hiro_budget.gui.navigation import set_glyph  # (navigation uses widgets)

        set_glyph(self, ICON_CALENDAR, "📅")

        self.popup = QFrame(self, Qt.WindowType.Popup)
        self.popup.setObjectName("dateRangePopup")
        layout = QVBoxLayout(self.popup)
        layout.setContentsMargins(12, 10, 12, 12)
        layout.addWidget(caption(self.tr("Period")))
        presets = QGridLayout()
        presets.setSpacing(6)
        today = date.today()
        last_month = _month_start(today) - timedelta(days=1)
        choices = [
            (self.tr("This month"), lambda: (_month_start(today), _month_end(today))),
            (
                self.tr("Last month"),
                lambda: (_month_start(last_month), _month_end(last_month)),
            ),
            (self.tr("This year"), lambda: (date(today.year, 1, 1), date(today.year, 12, 31))),
            (
                self.tr("Last year"),
                lambda: (date(today.year - 1, 1, 1), date(today.year - 1, 12, 31)),
            ),
            (
                self.tr("Last 12 months"),
                lambda: (_month_start(today.replace(year=today.year - 1, day=1)), today),
            ),
            (self.tr("Everything"), self.full_range),
        ]
        self.presets: dict[str, QPushButton] = {}
        for i, (label, span) in enumerate(choices):
            button = QPushButton(label)
            button.setObjectName("preset")
            button.clicked.connect(lambda _=False, span=span: self._choose(span()))
            presets.addWidget(button, i // 2, i % 2)
            self.presets[label] = button
        layout.addLayout(presets)
        layout.addSpacing(4)
        own = QHBoxLayout()  # or any range: the page's own date fields
        own.addWidget(QLabel(self.tr("From")))
        own.addWidget(date_from, 1)
        own.addWidget(QLabel(self.tr("to")))
        own.addWidget(date_to, 1)
        layout.addLayout(own)

        self.clicked.connect(self.open_popup)
        for edit in (date_from, date_to):
            edit.dateChanged.connect(self.refresh)
        self.refresh()

    def open_popup(self) -> None:
        self.popup.adjustSize()
        below = self.mapToGlobal(QPoint(0, self.height() + 4))
        screen = self.screen().availableGeometry()
        x = min(below.x(), screen.right() - self.popup.width())
        self.popup.move(max(screen.left(), x), below.y())
        self.popup.show()
        self.date_from.setFocus()

    def _choose(self, span: tuple[date, date] | None) -> None:
        """Set both dates, then filter once (each date field would filter on its own)."""
        if span is None:
            return
        first, last = span
        for edit, day in ((self.date_from, first), (self.date_to, last)):
            edit.blockSignals(True)
            edit.setDate(to_qdate(day))
            edit.blockSignals(False)
        self.refresh()
        self.date_to.dateChanged.emit(self.date_to.date())
        self.popup.hide()

    def refresh(self) -> None:
        """Show the dates (also after a page set them with signals blocked)."""
        first = from_qdate(self.date_from.date())
        last = from_qdate(self.date_to.date())
        self.setText(f"{fmt_date(first)} – {fmt_date(last)}")
        self.setToolTip(self.tr("Period: click to choose"))
