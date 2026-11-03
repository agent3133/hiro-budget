from datetime import date

import pytest
from PySide6.QtWidgets import QLabel

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


def test_overdue_fixed_costs_in_a_group_of_their_own(qtbot, imported) -> None:
    # #31: payments due in the past (not booked yet) looked like upcoming ones
    from datetime import timedelta

    from hiro_budget.services.recurring import Recurring

    w = MainWindow(imported)
    qtbot.addWidget(w)
    view = w.overview
    view.refresh()
    today = date.today()

    def payment(payee: str, due: date) -> Recurring:
        return Recurring(payee, "Abos", "monthly", 30, 10_00, due - timedelta(days=30), 5)

    o = view.data
    o.upcoming = [payment("Late", today - timedelta(days=3)), payment("Soon", today)]
    view._fill(o)
    tree = view.upcoming
    texts = [tree.topLevelItem(i).text(0) for i in range(tree.topLevelItemCount())]
    assert texts[0].startswith(("Overdue", "Überfällig")) and "(1)" in texts[0]
    assert tree.topLevelItem(1).text(1) == "Late" and tree.topLevelItem(1).toolTip(0)
    assert tree.topLevelItem(2).text(0).startswith(("Due in", "Fällig"))
    assert tree.topLevelItem(3).text(1) == "Soon"
    # nothing overdue: no headings, as before
    o.upcoming = [payment("Soon", today)]
    view._fill(o)
    assert tree.topLevelItemCount() == 1


def test_accounts_without_bookings_for_long_get_a_note(qtbot, imported, config_dir) -> None:
    # #46: an account whose last booking (or statement) is older than the limit of its
    # kind is listed under Notes, with a link to its bookings; the reminder can be off
    from hiro_budget.config import load_settings
    from hiro_budget.services import config_edit

    with imported.session() as s:
        assert overview.overview(s, imported.settings, date(2026, 3, 31)).stale == []
        later = overview.overview(s, imported.settings, date(2026, 6, 30))
    giro = next(a for a in later.stale if a.name == "ING Girokonto")
    assert giro.last <= date(2026, 3, 31) and giro.days == (date(2026, 6, 30) - giro.last).days
    assert all(a.days > overview.STALE_DAYS[overview.AccountKind.CASH] - 7 for a in later.stale)

    w = MainWindow(imported)
    qtbot.addWidget(w)
    w.overview._fill(later)
    labels = [
        lb for lb in w.overview.notes_card.findChildren(QLabel) if "ING Girokonto" in lb.text()
    ]
    assert labels and "href" in labels[0].text()
    labels[0].linkActivated.emit("#")
    assert w.tabs.currentWidget() is w.transactions
    assert w.transactions.current_filter().account_id == giro.account_id

    accounts = [a.model_copy() for a in imported.settings.app.own_accounts]
    next(a for a in accounts if a.name == "ING Girokonto").stale_hint = False
    config_edit.save_accounts(imported.settings, accounts)
    assert not next(
        a for a in load_settings(config_dir).app.own_accounts if a.name == "ING Girokonto"
    ).stale_hint
    with imported.session() as s:
        quiet = overview.overview(s, imported.settings, date(2026, 6, 30))
    assert "ING Girokonto" not in {a.name for a in quiet.stale}


def test_reminder_checkbox_in_the_accounts_editor(qtbot, imported, config_dir, monkeypatch) -> None:
    from PySide6.QtWidgets import QMessageBox

    from hiro_budget.config import load_settings

    for name in ("information", "warning", "critical", "question"):
        monkeypatch.setattr(QMessageBox, name, lambda *a, **k: QMessageBox.StandardButton.Yes)
    w = MainWindow(imported)
    qtbot.addWidget(w)
    page = w.accounts
    page.select_account("Bargeld")
    assert page.stale_hint.isChecked()
    page.stale_hint.setChecked(False)
    assert page.has_unsaved_changes()
    page.save()
    saved = next(a for a in load_settings(config_dir).app.own_accounts if a.name == "Bargeld")
    assert saved.stale_hint is False
    page.select_account("ING Girokonto")
    assert page.stale_hint.isChecked()
    page.select_account("Bargeld")
    assert not page.stale_hint.isChecked()
