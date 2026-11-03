"""Running balance in Bookings and the balance chart on Accounts (#45)."""

import pytest
from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication, QMessageBox
from sqlalchemy import select

from hiro_budget.db.models import Account, ImportBatch, Transaction
from hiro_budget.gui.main_window import MainWindow
from hiro_budget.gui.models.transaction_table_model import Col
from hiro_budget.services import accounts as acc_service
from hiro_budget.services import import_service
from hiro_budget.services import transactions as txs
from hiro_budget.services.networth import running_balances


@pytest.fixture(autouse=True)
def no_message_boxes(monkeypatch):
    for name in ("information", "warning", "critical"):
        monkeypatch.setattr(QMessageBox, name, lambda *a, **k: QMessageBox.StandardButton.Ok)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.No)


@pytest.fixture
def imported(app, statement_pdf):
    with app.session() as s:
        import_service.import_file(s, app.settings, statement_pdf)
    return app


def test_running_balance_ends_at_the_statement_and_the_account_balance(imported) -> None:
    with imported.session() as s:
        batch = s.scalars(select(ImportBatch)).first()
        account = batch.account_id
        balances = running_balances(s, account)
        rows = s.execute(
            select(Transaction.id, Transaction.amount_cents)
            .where(Transaction.account_id == account)
            .order_by(Transaction.booking_date, Transaction.id)
        ).all()
        assert set(balances) == {i for i, _ in rows}
        last_id = rows[-1][0]
        assert balances[last_id] == batch.closing_balance_cents
        now = next(b for b in acc_service.account_balances(s) if b.account_id == account)
        assert balances[last_id] == now.balance_cents
        # each booking: the balance before it plus its amount
        for (prev, _), (tx_id, cents) in zip(rows, rows[1:], strict=False):
            assert balances[tx_id] == balances[prev] + cents
        # a manual booking on a day with others: in booking order within the day
        day = s.get(Transaction, rows[1][0]).booking_date
        new = txs.create_manual(s, imported.settings, txs.ManualEntry(account, day, -1_00, "Kiosk"))
        same_day = [i for i, _ in rows if s.get(Transaction, i).booking_date == day]
        after = running_balances(s, account)
        assert after[new] == after[same_day[-1]] - 1_00  # the day's last (highest id)
        assert after[last_id] == balances[last_id] - 1_00  # and everything after it


def test_balance_column_only_with_one_account(qtbot, imported) -> None:
    w = MainWindow(imported)
    qtbot.addWidget(w)
    view = w.transactions
    assert view.table.isColumnHidden(Col.BALANCE)
    with imported.session() as s:
        account = s.scalars(select(ImportBatch.account_id)).first()
    view.show_account(account)
    assert not view.table.isColumnHidden(Col.BALANCE)
    header = view.table.horizontalHeader()
    assert header.visualIndex(Col.BALANCE) == header.visualIndex(Col.AMOUNT) + 1
    newest = view.model.rows()[0]
    index = view.model.index(0, Col.BALANCE)
    with imported.session() as s:
        expected = running_balances(s, account)[newest.id]
    assert index.data() == view.model.display(newest, Col.BALANCE) and expected is not None
    view.show_selection(txs.ALL)  # all accounts again
    assert view.table.isColumnHidden(Col.BALANCE)


def test_account_balance_chart_and_double_click(qtbot, imported) -> None:
    w = MainWindow(imported)
    qtbot.addWidget(w)
    page = w.accounts
    with imported.session() as s:
        giro = s.scalar(select(Account).join(ImportBatch).limit(1))
        name, account = giro.name, giro.id
    page.select_account(name)
    chart = page.balance_chart.chart()
    assert name in chart.title()
    (line,) = chart.series()
    assert line.count() >= 1
    viewport = page.balance_chart.viewport()
    event = QMouseEvent(
        QEvent.Type.MouseButtonDblClick,
        QPointF(10, 10),
        QPointF(10, 10),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    QApplication.sendEvent(viewport, event)
    assert w.tabs.currentWidget() is w.transactions
    assert w.transactions.current_filter().account_id == account
