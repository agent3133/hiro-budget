"""A new user sets the app up, step by step, through the windows where a user would (#25).

From an empty folder: first start, accounts, imports of statements and CSV exports, undoing
an import, categorizing, a first budget, an own plugin, a bank fetch, and a restart. The
steps share one app folder and run in order; after a failing step the later ones are
skipped. Synthetic data only (tests/fixtures, the CSV helpers of test_csv_import).
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QMessageBox
from sqlalchemy import func, select

from hiro_budget.app import App
from hiro_budget.config import CONFIG_FILES, load_settings
from hiro_budget.db.models import Account, LinkedPayment, Transaction
from hiro_budget.importers import plugins
from hiro_budget.services.import_service import OverlapMode
from tests.conftest import write_statement_pdf
from tests.fixtures import ing_statement as fx
from tests.fixtures import revolut as rfx
from tests.test_csv_import import EMAIL, PAYPAL_ROWS, ing_csv, paypal_csv, revolut_csv
from tests.test_plugin_docs import block, sample_pages

pytestmark = pytest.mark.incremental


class Home:
    """The new user's folder and what the steps hand on to each other."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.config = root / "config"
        self.files = root / "statements"
        self.app: App | None = None
        self.counts: dict[str, int] = {}


@pytest.fixture(scope="module")
def home(tmp_path_factory) -> Home:
    plugins.reset()
    h = Home(tmp_path_factory.mktemp("new_user"))
    h.files.mkdir()
    yield h
    plugins.reset()


@pytest.fixture(autouse=True)
def quiet_message_boxes(monkeypatch):
    """Information and warnings are collected; questions are answered with Yes."""
    shown: list[str] = []
    for name in ("information", "warning", "critical"):
        monkeypatch.setattr(
            QMessageBox, name, lambda *a, **k: shown.append(a[2]) or QMessageBox.StandardButton.Ok
        )
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    return shown


def bookings(app: App, account: str | None = None) -> int:
    with app.session() as s:
        q = select(func.count()).select_from(Transaction).join(Account)
        if account:
            q = q.where(Account.name == account)
        return s.scalar(q)


def main_window(qtbot, app: App):
    from hiro_budget.gui.main_window import MainWindow

    w = MainWindow(app)
    qtbot.addWidget(w)
    return w


def import_files(qtbot, app: App, paths: list[Path], monkeypatch) -> str:
    """The import dialog with these files; overlaps: "Add missing" for all."""
    from hiro_budget.gui.import_dialog import ImportDialog

    monkeypatch.setattr(ImportDialog, "ask_overlap", lambda self, p: (OverlapMode.ADD, True))
    shown: list[str] = []
    monkeypatch.setattr(QMessageBox, "information", lambda _p, _t, text: shown.append(text))
    dlg = ImportDialog(app, paths=paths)
    qtbot.addWidget(dlg)
    assert dlg.import_button.isEnabled()
    dlg._import()
    return shown[-1]


# -- 0. first start -------------------------------------------------------------------------


def test_0_first_start_creates_the_configuration(qtbot, home):
    assert not home.config.exists()
    home.app = App(load_settings(home.config))
    assert sorted(p.name for p in home.config.iterdir()) == sorted(CONFIG_FILES)
    w = main_window(qtbot, home.app)
    assert w.isWindow()
    assert home.app.settings.categories.full_keys()  # the example categories
    assert bookings(home.app) == 0


# -- 1. accounts ----------------------------------------------------------------------------


def test_1_accounts_page(qtbot, home):
    w = main_window(qtbot, home.app)
    acc = w.accounts
    for name, iban in (("ING Girokonto", "DE12 3456 7890 1234 5678 90"), ("Revolut", rfx.IBAN)):
        acc.select_account(name)  # the example accounts get the real (test) IBANs
        acc.iban.setText(iban)
        acc.iban.textEdited.emit(iban)
    acc._add()
    acc.name.setText("PayPal")
    acc.name.textEdited.emit("PayPal")
    acc.kind.setCurrentIndex(acc.kind.findData("wallet"))
    acc.email.setText(EMAIL)
    acc.email.textEdited.emit(EMAIL)
    acc.save()
    assert not acc.has_unsaved_changes()
    saved = {a.name: a for a in load_settings(home.config).app.own_accounts}
    assert saved["ING Girokonto"].iban == "DE12345678901234567890"
    assert (saved["PayPal"].kind, saved["PayPal"].email) == ("wallet", EMAIL)


# -- 2. imports -----------------------------------------------------------------------------


def test_2_import_statements_and_exports(qtbot, home, monkeypatch):
    from hiro_budget.gui.import_dialog import ImportDialog
    from hiro_budget.plugins.ing_giro_pdf.importer import parse_pages
    from hiro_budget.plugins.revolut_pdf import importer as revolut_pdf

    f = home.files
    statement = parse_pages(fx.PAGES)
    write_statement_pdf(f / "ing_2026-03.pdf", fx.PAGES)
    (f / "umsatzanzeige.csv").write_text(
        ing_csv(statement.transactions, fx.OPENING_CENTS), encoding="latin-1"
    )
    (f / "paypal.csv").write_text(paypal_csv(PAYPAL_ROWS), encoding="utf-8-sig")
    revolut = revolut_pdf.parse_pages(rfx.PAGES)
    (f / "revolut.pdf").write_bytes(b"%PDF-")  # its text comes from the fixture
    monkeypatch.setattr(revolut_pdf, "extract_pages", lambda _path: rfx.PAGES)
    monkeypatch.setattr(
        revolut_pdf.RevolutPdfImporter, "detect", lambda self, p: p.name == "revolut.pdf"
    )
    later = ("Card Payment", date(2026, 12, 1), "Cloud Storage Ltd", -744, 7, "COMPLETED")
    (f / "revolut.csv").write_text(
        revolut_csv(revolut.transactions, revolut.opening_balance_cents, [later]),
        encoding="utf-8",
    )
    order = ["ing_2026-03.pdf", "umsatzanzeige.csv", "paypal.csv", "revolut.pdf", "revolut.csv"]
    message = import_files(qtbot, home.app, [f / n for n in order], monkeypatch)

    assert bookings(home.app, "ING Girokonto") == fx.BOOKING_COUNT  # the CSV: duplicates
    assert bookings(home.app, "Revolut") == rfx.BOOKING_COUNT + 1  # + the CSV's newer one
    assert bookings(home.app, "PayPal") == 3  # only what did not go through the Giro
    with home.app.session() as s:
        linked = s.scalar(
            select(func.count())
            .select_from(LinkedPayment)
            .where(LinkedPayment.transaction_id.is_not(None))
        )
    assert linked == 3  # PayPal payments shown on their Giro bookings
    assert "3 payment(s) linked to their bank booking." in message
    backups = home.app.settings.db_path.parent / "backups"
    assert any(backups.iterdir())  # a backup before the import
    # all files are imported now: the folder offers nothing new
    dlg = ImportDialog(home.app)
    qtbot.addWidget(dlg)
    from hiro_budget.services import import_service

    with home.app.session() as s:
        assert import_service.new_statement_files(s, f) == []
    home.counts["after_import"] = bookings(home.app)


def test_3_undo_an_import_and_import_again(qtbot, home, monkeypatch):
    from hiro_budget.gui.import_history_dialog import ImportHistoryDialog

    dlg = ImportHistoryDialog(home.app)
    qtbot.addWidget(dlg)
    row = next(
        r for r in range(dlg.table.rowCount()) if dlg.table.item(r, 1).text() == "revolut.csv"
    )
    dlg.table.selectRow(row)
    dlg._delete()
    assert bookings(home.app) == home.counts["after_import"] - 1
    message = import_files(qtbot, home.app, [home.files / "revolut.csv"], monkeypatch)
    assert message.startswith("1 bookings imported")
    assert bookings(home.app) == home.counts["after_import"]


# -- 3. categorizing and the first budget -----------------------------------------------------


def test_4_categorize(qtbot, home, monkeypatch):
    from hiro_budget.gui.split_dialog import SplitDialog

    w = main_window(qtbot, home.app)
    view = w.transactions
    rows = view.model.rows()
    category = next(c for c in view.choices if c.category_id is not None)
    first = next(r for r in rows if r.is_uncategorized and r.amount_cents < 0)
    view.assign(first.id, category)
    assert not next(r for r in view.model.rows() if r.id == first.id).is_uncategorized

    rent = next(r for r in view.model.rows() if r.amount_cents == -120_00)
    cats = [c for c in view.choices if c.category_id is not None][:3]

    def split_in_three(dlg):
        dlg._add_row()
        for i, (cents, choice) in enumerate(zip((-100_00, -15_00, -5_00), cats, strict=True)):
            dlg.table.cellWidget(i, 1).setValue(cents / 100)
            dlg.table.cellWidget(i, 0).setCurrentIndex(dlg.table.cellWidget(i, 0).findData(choice))
        dlg._accept()
        return 1

    monkeypatch.setattr(SplitDialog, "exec", split_in_three)
    view.split(rent)
    split = next(r for r in view.model.rows() if r.id == rent.id)
    assert [s.amount_cents for s in split.splits] == [-100_00, -15_00, -5_00]

    rule_from = next(r for r in view.model.rows() if r.payee == "Handwerker GmbH")
    w._create_rule(rule_from)
    editor = w.config_view.rules
    key = home.app.settings.categories.full_keys()[0]
    editor.category.setCurrentIndex(editor.category.findData(key))
    editor.save()
    assert any(r.match.counterparty for r in load_settings(home.config).rules.rules)
    w.recategorize()
    w.retrain()


def test_5_first_budget_and_reports(qtbot, home):
    w = main_window(qtbot, home.app)
    from tests.test_gui_budget import cat

    budget = w.budget
    groceries = cat(home.app, "lebensmittel.supermarkt")  # an example category
    before = budget.data.ready_to_assign
    budget.set_assigned(groceries, 50_00)
    assert budget.category(groceries).assigned == 50_00
    assert budget.data.ready_to_assign == before - 50_00
    w.tabs.setCurrentWidget(w.reports)
    w.reports.refresh()
    assert w.reports.totals.text()


# -- 4. an own plugin -----------------------------------------------------------------------


def test_6_own_plugins(qtbot, home, monkeypatch):
    from hiro_budget.gui.plugins_dialog import COL_NAME, PluginsDialog

    plugin = home.config / "plugins" / "musterbank_pdf"  # the example of docs/plugins.llm.md
    plugin.mkdir(parents=True)
    (plugin / "plugin.yaml").write_text(block("## 4. The API", "yaml"), encoding="utf-8")
    (plugin / "importer.py").write_text(block("## 5. Worked example", "python"), "utf-8")
    write_statement_pdf(home.files / "musterbank.pdf", sample_pages())

    dlg = PluginsDialog(home.app)
    qtbot.addWidget(dlg)
    dlg._reload()
    row = next(r for r in range(dlg.table.rowCount()) if dlg.table.item(r, 0).text() == plugin.name)
    assert dlg.table.item(row, 4).text() == "off: contains program code"
    dlg.table.item(row, COL_NAME).setCheckState(Qt.CheckState.Checked)
    dlg._save()  # the question about program code: Yes
    assert load_settings(home.config).app.plugins.enabled == ["musterbank_pdf"]

    import_files(qtbot, home.app, [home.files / "musterbank.pdf"], monkeypatch)
    assert bookings(home.app, "Musterbank Girokonto") == 3


# -- 5. fetching from the bank --------------------------------------------------------------


def test_7_fetch_from_the_bank(home):
    from hiro_budget.services.bank import fetch
    from tests.test_bank_fetch import LOGIN, FakeBank, fetched, link, tx

    settings = home.app.settings
    fake = FakeBank()
    with home.app.session() as s:
        account = s.scalar(select(Account).where(Account.name == "ING Girokonto"))
        cut = fetch.suggested_fetch_from(s, account.id)
    giro = link("ING Girokonto", fetch_from=cut)
    settings.app.fints.logins = [LOGIN.model_copy(update={"accounts": [giro]})]
    fake.bookings[giro.bank_id] = [tx(date(2026, 4, 2), -50_00), tx(date(2026, 4, 3), -9_09)]
    before = bookings(home.app, "ING Girokonto")
    with home.app.session() as s:
        result = fetch.import_fetched(s, settings, fetched(fake, [giro], s), retrain=False)
    assert result.added == 2
    assert bookings(home.app, "ING Girokonto") == before + 2
    home.counts["final"] = bookings(home.app)


# -- 6. the next day ------------------------------------------------------------------------


def test_8_restart_keeps_everything(qtbot, home):
    plugins.reset()
    app = App(load_settings(home.config))
    assert bookings(app) == home.counts["final"]
    s = app.settings
    assert {a.name for a in s.app.own_accounts} >= {"ING Girokonto", "PayPal", "Revolut"}
    assert s.app.plugins.enabled == ["musterbank_pdf"]
    w = main_window(qtbot, app)
    assert w.transactions.model.rowCount() > 0
