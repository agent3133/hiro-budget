"""Breakdowns of charts and the Overview cards without the mouse (#70)."""

from datetime import date

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import QApplication, QMenu

from hiro_budget.gui import charts, sankey
from tests.test_cash_flow import APRIL, flow_app  # noqa: F401 (fixture)
from tests.test_drilldown import app as drill_app  # noqa: F401 (fixture)
from tests.test_keyboard import SHIFT, no_message_boxes, press, window  # noqa: F401


@pytest.fixture
def menus(monkeypatch):
    """Menus are recorded instead of waiting for a click (patching exec has no effect)."""
    shown: list[QMenu] = []

    class RecordingMenu(QMenu):
        def exec(self, *args):
            shown.append(self)

    monkeypatch.setattr(charts, "QMenu", RecordingMenu)
    monkeypatch.setattr(sankey, "QMenu", RecordingMenu)
    return shown


def entries(menu: QMenu) -> list:
    """The menu's actions, those of its submenus in place of the submenus."""
    out = []
    for action in menu.actions():
        if action.menu() is not None:
            out += entries(action.menu())
        elif not action.isSeparator():
            out.append(action)
    return out


def test_bar_chart_menu_opens_the_breakdown(qtbot, drill_app) -> None:  # noqa: F811
    from hiro_budget.gui.reports import SavingsRateReport

    page = SavingsRateReport(drill_app)
    qtbot.addWidget(page)
    page.refresh(date(2026, 3, 1), date(2026, 4, 30), None, one_offs=False)
    groups = charts.drill_entries(page.view.chart())
    assert groups  # a bar set with a double-click
    label, action = groups[0][1][-1]
    assert "04" in label  # named like the axis: the month
    action()
    assert page.breakdown_dialog is not None


def test_shift_f10_on_a_chart(qtbot, window, menus) -> None:  # noqa: F811
    press(qtbot, Qt.Key.Key_5, Qt.KeyboardModifier.ControlModifier)
    view = window.reports.monthly_view
    view.setFocus()
    qtbot.waitUntil(view.hasFocus)
    press(qtbot, Qt.Key.Key_F10, SHIFT)
    qtbot.keyRelease(QApplication.focusWidget(), Qt.Key.Key_Shift)
    assert len(menus) == 1
    actions = entries(menus[0])
    assert actions
    actions[0].trigger()
    assert window.reports.breakdown_dialog is not None


def test_sankey_menu(qtbot, flow_app, menus) -> None:  # noqa: F811
    from hiro_budget.gui.reports import CashFlowReport

    page = CashFlowReport(flow_app)
    qtbot.addWidget(page)
    page.refresh(*APRIL, None)
    assert page.chart.focusPolicy() == Qt.FocusPolicy.StrongFocus
    page.chart.drill_menu(QPoint(10, 10))
    actions = entries(menus[0])
    assert len(actions) == len(page.chart.nodes)
    with qtbot.waitSignal(page.chart.node_activated) as signal:
        actions[0].trigger()
    assert signal.args[0] in range(len(page.chart.nodes))


def test_overview_cards_by_keyboard(qtbot, window) -> None:  # noqa: F811
    card = window.overview.cards["uncategorized"]
    assert card.focusPolicy() == Qt.FocusPolicy.StrongFocus
    card.setFocus()
    qtbot.waitUntil(card.hasFocus)
    press(qtbot, Qt.Key.Key_Return)
    assert window.tabs.currentWidget() is window.transactions
