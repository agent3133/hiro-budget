"""Fetch button / status and the bank access dialog, against the fake bank."""

from __future__ import annotations

from datetime import date

import pytest
from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QApplication, QMessageBox, QWidget
from sqlalchemy import func, select

from hiro_budget.config import BankLogin, FintsConfig, load_settings
from hiro_budget.db.models import ImportBatch, Transaction
from hiro_budget.gui import bank_dialog, bank_fetch
from hiro_budget.services import bank, import_service
from tests.test_bank_fetch import IBAN, FakeBank, link, tx


@pytest.fixture
def english(qapp):
    """The English translation (plural forms of "%n ...(s)")."""
    from hiro_budget.gui.main import install_translators

    translators = install_translators(QApplication.instance(), "en")
    yield
    for t in translators:
        QCoreApplication.removeTranslator(t)


def setup(app, monkeypatch, pins: list[str | None]) -> tuple[FakeBank, list[bool]]:
    fake = FakeBank()
    monkeypatch.setattr(bank, "connect", fake.connect)
    rejected: list[bool] = []

    def ask_pin(parent, login, was_rejected=False):
        rejected.append(was_rejected)
        return pins.pop(0)

    monkeypatch.setattr(bank_fetch, "ask_pin", ask_pin)
    monkeypatch.setattr(bank_dialog, "ask_pin", ask_pin)
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)
    return fake, rejected


def configure(app, *links) -> None:
    app.settings.app.fints = FintsConfig(logins=[BankLogin(login_id="test", accounts=list(links))])


def control(app, qtbot) -> bank_fetch.FetchControl:
    window = QWidget()
    qtbot.addWidget(window)
    return bank_fetch.FetchControl(app, window)


def test_without_bank_login_the_settings_open(app, qtbot, monkeypatch):
    setup(app, monkeypatch, [])
    fc = control(app, qtbot)
    with qtbot.waitSignal(fc.setup_needed, timeout=1000):
        fc.button.click()


def test_fetch_imports_and_reports(app, qtbot, monkeypatch, statement_pdf, english):
    fake, rejected = setup(app, monkeypatch, ["wrong", "1234"])
    with app.session() as s:
        import_service.import_file(s, app.settings, statement_pdf)
    configure(app, link("ING Girokonto", fetch_from=date(2026, 4, 1)))
    fake.bookings[IBAN] = [tx(date(2026, 4, 2), -5000)]
    fake.balances[IBAN] = 355587 - 5000
    fc = control(app, qtbot)
    with qtbot.waitSignal(fc.fetched, timeout=10000):
        fc.button.click()
    assert rejected == [False, True]  # the rejected PIN was asked again
    assert fc.status.text() == "✓ 1 new booking · balances match"
    assert "ING Girokonto: 1 new booking" in fc.details
    assert not fc.running
    with app.session() as s:
        fetched = select(func.count()).select_from(Transaction).join(ImportBatch)
        assert s.scalar(fetched.where(ImportBatch.importer == "fints")) == 1


def test_balance_difference_shows_a_notice(app, qtbot, monkeypatch, english):
    fake, _ = setup(app, monkeypatch, ["1234", "1234"])
    configure(app, link("ING Girokonto", fetch_from=date(2026, 4, 1)))
    fake.bookings[IBAN] = [tx(date(2026, 4, 2), -5000)]
    fake.balances[IBAN] = -5000
    fc = control(app, qtbot)
    with qtbot.waitSignal(fc.fetched, timeout=10000):
        fc.button.click()
    fake.balances[IBAN] = -5100
    fc.button.click()
    qtbot.waitUntil(lambda: not fc.running and "differs" in fc.status.text(), timeout=10000)
    assert fc.status.text().startswith("⚠ 0 new bookings")
    assert "balance at the bank" in fc.details


def test_confirmation_in_the_app_can_be_cancelled(app, qtbot, monkeypatch):
    fake, _ = setup(app, monkeypatch, ["1234"])
    fake.confirmations = 10_000  # never confirmed
    monkeypatch.setattr(bank_fetch, "POLL_SECONDS", 0.01)
    configure(app, link("ING Girokonto"))
    fc = control(app, qtbot)
    fc.button.click()
    qtbot.waitUntil(lambda: not fc.cancel_button.isHidden(), timeout=5000)
    assert fc.status.text() == "Please confirm in the ING app"
    fc.cancel_button.click()
    qtbot.waitUntil(lambda: not fc.running, timeout=5000)
    assert fc.status.text() == "Fetch cancelled"
    assert fc.cancel_button.isHidden()


def test_pin_prompt_cancelled(app, qtbot, monkeypatch):
    setup(app, monkeypatch, [None])
    configure(app, link("ING Girokonto"))
    fc = control(app, qtbot)
    fc.button.click()
    assert fc.status.text() == "Fetch cancelled"
    assert not fc.running


def test_offline(app, qtbot, monkeypatch):
    setup(app, monkeypatch, ["1234"])

    def offline(*a):
        raise bank.Offline("no route to host")

    monkeypatch.setattr(bank, "connect", offline)
    configure(app, link("ING Girokonto"))
    fc = control(app, qtbot)
    fc.button.click()
    qtbot.waitUntil(lambda: not fc.running, timeout=5000)
    assert fc.status.text() == "Bank not reachable"


def test_bank_access_dialog_lists_and_saves_the_accounts(app, qtbot, monkeypatch, statement_pdf):
    setup(app, monkeypatch, ["1234"])
    with app.session() as s:
        import_service.import_file(s, app.settings, statement_pdf)
    dlg = bank_dialog.BankAccessDialog(app)
    qtbot.addWidget(dlg)
    assert "placeholder" in dlg.product_hint.text()
    dlg.login_id.setText("123456")
    dlg.test_button.click()
    qtbot.waitUntil(lambda: dlg.table.rowCount() == 3, timeout=5000)
    assert dlg.test_status.text().startswith("✓ Connected: 3 accounts")
    giro, extra, depot = (dlg.table.cellWidget(r, 2) for r in range(3))
    assert giro.currentData() == "ING Girokonto"  # same IBAN
    assert extra.currentText() == "New: ING Extra-Konto 9999"  # the name is taken
    assert dlg.table.cellWidget(0, 3).date().toPython() == date(2026, 4, 1)
    assert dlg.table.cellWidget(1, 3).specialValueText() == dlg.table.cellWidget(1, 3).text()
    assert dlg.table.cellWidget(2, 3) is None  # depot: holdings later
    dlg._save()
    fints = load_settings(app.settings.config_dir).app.fints
    assert fints.product_id is None
    login = fints.logins[0]
    assert (login.login_id, login.bank_code) == ("123456", "50010517")
    assert [(lk.account, lk.kind, lk.fetch_from) for lk in login.accounts] == [
        ("ING Girokonto", "giro", date(2026, 4, 1)),
        ("ING Extra-Konto 9999", "savings", None),
        ("ING Direkt-Depot", "depot", None),
    ]
    assert "pin" not in app.settings.config_file.read_text("utf-8").lower()


def test_bank_access_dialog_refuses_two_accounts_into_one(app, qtbot, monkeypatch):
    setup(app, monkeypatch, [])
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warnings.append(a[2]))
    configure(
        app,
        link("ING Girokonto"),
        link("ING Girokonto", "DE99999999999999999999", "savings"),
    )
    dlg = bank_dialog.BankAccessDialog(app)
    qtbot.addWidget(dlg)
    dlg._save()
    assert warnings and "ING Girokonto" in warnings[0]
    assert dlg.result() == 0


def test_bank_access_dialog_add_and_remove_logins(app, qtbot, monkeypatch):
    setup(app, monkeypatch, [])
    configure(app, link("ING Girokonto"))
    dlg = bank_dialog.BankAccessDialog(app)
    qtbot.addWidget(dlg)
    assert (dlg.login_list.count(), dlg.table.rowCount()) == (1, 1)
    dlg.add_button.click()
    assert (dlg.login_list.count(), dlg.table.rowCount()) == (2, 0)
    assert dlg.login_id.text() == ""
    dlg.name.setText("Sparkasse")
    dlg.login_list.setCurrentIndex(0)  # back to ING: the new one keeps its name
    assert dlg.login_list.itemText(1) == "Sparkasse"
    assert dlg.table.rowCount() == 1
    dlg.remove_button.click()  # ING
    assert dlg.name.text() == "Sparkasse"
    dlg.remove_button.click()
    assert not dlg.detail.isEnabled()
    dlg._save()
    assert load_settings(app.settings.config_dir).app.fints.logins == []
