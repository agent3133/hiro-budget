"""Back / forward between the places visited, as in a browser.

A place is a page with what it showed when it was left (``nav_state`` of the page: the
bookings filter, the budget month, the selected account, ...). Going back restores it
with ``restore_nav_state``.
"""

from __future__ import annotations

from PySide6.QtCore import QObject, Signal

from hiro_budget.gui.navigation import NavigationView

LIMIT = 50  # places kept


class History(QObject):
    #: the places before or after the current one changed (buttons on / off)
    changed = Signal()

    def __init__(self, tabs: NavigationView, parent=None) -> None:
        super().__init__(parent)
        self.tabs = tabs
        self.places: list[list] = []  # [page index, state when it was left]
        self.pos = -1
        self._moving = False
        tabs.currentChanged.connect(self._visited)
        self._visited(tabs.currentIndex())

    def can_go_back(self) -> bool:
        return self.pos > 0

    def can_go_forward(self) -> bool:
        return self.pos < len(self.places) - 1

    def back(self) -> None:
        if self.can_go_back():
            self._go(self.pos - 1)

    def forward(self) -> None:
        if self.can_go_forward():
            self._go(self.pos + 1)

    def _capture(self) -> None:
        """What the current page shows, before leaving it."""
        place = self.places[self.pos]
        page = self.tabs.widget(place[0])
        place[1] = page.nav_state() if hasattr(page, "nav_state") else None

    def _visited(self, index: int) -> None:
        if self._moving or index < 0:
            return
        if self.pos >= 0:
            if self.places[self.pos][0] == index:
                return
            self._capture()
        del self.places[self.pos + 1 :]  # a new way: the old forward places are gone
        self.places.append([index, None])
        del self.places[:-LIMIT]
        self.pos = len(self.places) - 1
        self.changed.emit()

    def _go(self, pos: int) -> None:
        self._capture()
        self.pos = pos
        index, state = self.places[pos]
        self._moving = True
        try:
            self.tabs.setCurrentIndex(index)
            page = self.tabs.widget(index)
            if state is not None and hasattr(page, "restore_nav_state"):
                page.restore_nav_state(state)
        finally:
            self._moving = False
        self.changed.emit()
