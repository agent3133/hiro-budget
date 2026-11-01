from datetime import date

import pytest

from hiro_budget.gui.main_window import MainWindow
from hiro_budget.services import budget, import_service, overview
from hiro_budget.services import transactions as txs


@pytest.fixture
def imported(app, statement_pdf):
    with app.session() as s:
        import_service.import_file(s, app.settings, statement_pdf)
    return app


def test_overview_numbers(imported) -> None:
    today = date(2026, 3, 31)
    with imported.session() as s:
        o = overview.overview(s, imported.settings, today)
        month = budget.compute_month(s, imported.settings, "2026-03")
        assert o.month == "2026-03"
        assert o.ready_to_assign == month.ready_to_assign
        assert o.uncategorized == txs.count_transactions(
            s, txs.TxFilter(category=txs.UNCATEGORIZED)
        )
    assert o.income > 0 and o.saved == o.income - o.expenses
    assert o.net_worth and o.net_worth[-1][0] == "2026-03"
    assert o.gaps == []  # one statement: nothing missing


def test_start_page_cards_lead_to_their_pages(qtbot, imported) -> None:
    w = MainWindow(imported)
    qtbot.addWidget(w)
    view = w.overview
    assert w.tabs.widget(0) is view
    view.refresh()
    cards = view.cards
    assert cards["uncategorized"].value.text() == "3" and cards["uncategorized"].state == "warn"
    assert cards["assign"].state in ("pos", "neg", "")
    cards["uncategorized"].clicked.emit()
    assert w.tabs.currentWidget() is w.transactions
    assert w.transactions.current_filter().category == txs.UNCATEGORIZED
    cards["assign"].clicked.emit()
    assert w.tabs.currentWidget() is w.budget
    cards["net_worth"].clicked.emit()
    assert w.tabs.currentWidget() is w.reports
    assert type(w.reports.tabs.currentWidget()).__name__ == "NetWorthReport"
    view.grab()  # chart, lists and banners draw
