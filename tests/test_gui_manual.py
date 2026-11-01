"""GUI smoke tests: manual bookings and the accounts tab (headless)."""

from datetime import date

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QMessageBox
from sqlalchemy import select

from hiro_budget.config import load_settings
from hiro_budget.db.models import Account
from hiro_budget.gui.booking_dialog import BookingDialog
from hiro_budget.gui.main_window import MainWindow
from hiro_budget.gui.models.transaction_table_model import Col
from hiro_budget.services import import_service
from hiro_budget.services import transactions as txs
from hiro_budget.services.transactions import ManualEntry


@pytest.fixture(autouse=True)
def no_message_boxes(monkeypatch):
    for name in ("information", "warning", "critical"):
        monkeypatch.setattr(QMessageBox, name, lambda *a, **k: QMessageBox.StandardButton.Ok)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)


@pytest.fixture
def window(qtbot, app, statement_pdf):
    with app.session() as s:
        import_service.import_file(s, app.settings, statement_pdf)
    w = MainWindow(app)
    qtbot.addWidget(w)
    return w


def acc_id(app, name: str) -> int:
    with app.session() as s:
        return s.scalar(select(Account.id).where(Account.name == name))


def test_booking_dialog_builds_entry(qtbot, app) -> None:
    dlg = BookingDialog(app, default_account_id=acc_id(app, "Bargeld"))
    qtbot.addWidget(dlg)
    assert dlg.account.currentText() == "Bargeld"
    dlg.amount.setValue(12.5)
    dlg.payee.setText("Wochenmarkt")
    dlg._accept()
    e = dlg.entry
    assert e.amount_cents == -1250 and e.payee == "Wochenmarkt"
    assert e.category_id is None and e.transfer_account_id is None  # automatic


def test_booking_dialog_validates(qtbot, app) -> None:
    dlg = BookingDialog(app)
    qtbot.addWidget(dlg)
    dlg._accept()
    assert dlg.entry is None and dlg.error.text()


def test_booking_dialog_income_and_transfer(qtbot, app) -> None:
    dlg = BookingDialog(app, default_account_id=acc_id(app, "Bargeld"))
    qtbot.addWidget(dlg)
    dlg.direction.setCurrentIndex(1)  # income
    dlg.amount.setValue(200)
    dlg.payee.setText("Geldautomat")
    savings = acc_id(app, "ING Extra-Konto")
    idx = next(
        i
        for i in range(dlg.category.count())
        if (c := dlg.category.itemData(i)) and c.transfer_account_id == savings
    )
    dlg.category.setCurrentIndex(idx)
    dlg._accept()
    assert dlg.entry.amount_cents == 200_00 and dlg.entry.transfer_account_id == savings


def test_new_booking_from_view(window, monkeypatch) -> None:
    view = window.transactions
    entry = ManualEntry(acc_id(window.app, "Bargeld"), date(2026, 3, 4), -899, "Eisdiele")

    def fake_exec(dlg):
        dlg.entry = entry
        return 1

    monkeypatch.setattr(BookingDialog, "exec", fake_exec)
    before = view.model.rowCount()
    view.new_booking()
    assert view.model.rowCount() == before + 1
    (row,) = view.selected_rows()
    assert row.payee == "Eisdiele" and row.is_manual_entry


def test_counter_booking_is_read_only(window) -> None:
    view = window.transactions
    loan_row = next(r for r in view.model.rows() if r.is_counterpart)
    i = view.model.rows().index(loan_row)
    flags = view.model.flags(view.model.index(i, Col.CATEGORY))
    assert not flags & Qt.ItemFlag.ItemIsEditable
    assert view.model.source_label(loan_row) == "counter-booking"


def test_delete_manual_booking_from_view(window) -> None:
    view = window.transactions
    with window.app.session() as s:
        tx_id = txs.create_manual(
            s,
            window.app.settings,
            ManualEntry(acc_id(window.app, "Bargeld"), date(2026, 3, 4), -100, "Kiosk"),
        )
    view.refresh()
    view.delete_bookings([tx_id])
    assert all(r.id != tx_id for r in view.model.rows())


def test_accounts_tab_shows_balances(window) -> None:
    acc = window.accounts
    names = [d.name for d in acc.drafts]
    assert {"ING Girokonto", "Bargeld", "ING Baufinanzierung"} <= set(names)
    row = names.index("Bargeld")
    balance = acc.table.topLevelItem(row).text(2)
    assert "100,00" in balance or "100.00" in balance
    # header cards: sums per kind of account, the total over all
    cards = acc.cards
    balances = {d.name: d.balance.balance_cents for d in acc.drafts if d.balance}
    assert cards["total"].cents == sum(balances.values())
    assert cards["loan"].cents == balances["ING Baufinanzierung"]
    assert not cards["loan"].isHidden() and cards["depot"].isHidden()  # no depot here
    assert "Bargeld" in cards["liquid"].toolTip()
    assert cards["liquid"].cents + cards["loan"].cents == cards["total"].cents


def test_accounts_tab_add_and_save(window, config_dir) -> None:
    acc = window.accounts
    acc._add()
    acc.name.setText("Kasse Urlaub")
    acc.name.textEdited.emit("Kasse Urlaub")
    acc.opening_balance.setValue(50)
    assert acc.has_unsaved_changes()
    acc.save()
    assert not acc.has_unsaved_changes()
    reloaded = load_settings(config_dir)
    new = next(a for a in reloaded.app.own_accounts if a.name == "Kasse Urlaub")
    assert new.kind == "cash" and new.opening_balance == 50.0
    # The new account is available in the transactions view right away.
    assert window.transactions.account.findText("Kasse Urlaub") >= 0


def test_accounts_tab_refuses_removing_used_account(window) -> None:
    acc = window.accounts
    acc.select_account("ING Girokonto")
    acc._remove()
    assert "ING Girokonto" in [d.name for d in acc.drafts]
