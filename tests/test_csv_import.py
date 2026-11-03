"""CSV import with configurable formats; PayPal payments linked to bank bookings (#21).
The formats are built-in plugins (#24)."""

from __future__ import annotations

import csv
import io
from pathlib import Path

import pytest
from sqlalchemy import func, select

from hiro_budget.config import CsvFormat, OwnAccount, RuleDef, RuleMatch
from hiro_budget.db.models import Account, LinkedPayment, SplitSource, Transaction
from hiro_budget.importers import plugins
from hiro_budget.importers.base import ImportError_
from hiro_budget.importers.csv_import import CsvImporter
from hiro_budget.plugins.ing_giro_pdf.importer import parse_pages
from hiro_budget.services import import_service
from hiro_budget.services.import_service import OverlapMode
from tests.conftest import REPO_ROOT
from tests.fixtures import ing_statement as fx

EMAIL = "familie@example.org"


def formats() -> list[CsvFormat]:
    """The CSV formats of the built-in plugins."""
    return plugins.discover(None).formats()


def fmt(name: str):
    return next(f for f in formats() if f.name == name)


def de(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    euros, rest = divmod(abs(cents), 100)
    return f"{sign}{euros:,}".replace(",", ".") + f",{rest:02d}"


# -- ING Umsatzanzeige CSV ------------------------------------------------------------------

# booking types as the CSV spells them (the format renames them to the PDF spelling)
CSV_TYPES = {"Dauerauftrag/Terminueberw.": "Dauerauftrag / Terminueberweisung"}


def ing_csv(transactions, opening_cents: int) -> str:
    """An ING CSV export of ``transactions`` (newest first, as ING writes it)."""
    out = io.StringIO()
    w = csv.writer(out, delimiter=";", lineterminator="\n")
    w.writerow(["Umsatzanzeige", "Datei erstellt am: 01.04.2026 10:00"])
    w.writerow([])
    w.writerow(["IBAN", "DE12 3456 7890 1234 5678 90"])
    w.writerow(["Kontoname", "Girokonto"])
    w.writerow(["Bank", "ING"])
    w.writerow([])
    header = [
        "Buchung",
        "Wertstellungsdatum",
        "Auftraggeber/Empfänger",
        "Buchungstext",
        "Verwendungszweck",
        "Referenz",
        "Saldo",
        "Währung",
        "Betrag",
        "Währung",
    ]
    w.writerow(header)
    balance, rows = opening_cents, []
    for t in transactions:
        balance += t.amount_cents
        rows.append(
            [
                f"{t.booking_date:%d.%m.%Y}",
                f"{(t.value_date or t.booking_date):%d.%m.%Y}",
                t.counterparty,
                CSV_TYPES.get(t.booking_type, t.booking_type),
                t.purpose,
                "",
                de(balance),
                "EUR",
                de(t.amount_cents),
                "EUR",
            ]
        )
    for row in reversed(rows):
        w.writerow(row)
    return out.getvalue()


@pytest.fixture
def statement():
    return parse_pages(fx.PAGES)


def test_misspelt_format_key_is_an_error(tmp_path):
    """A format of a user plugin with a misspelt key: the plugin reports it, nothing else."""
    builtin = plugins.builtin_dir() / "ing_umsatzanzeige_csv" / "format.csv.yaml"
    text = builtin.read_text(encoding="utf-8").replace('delimiter: ";"', 'delimeter: ";"', 1)
    folder = tmp_path / "plugins" / "my_bank_csv"
    folder.mkdir(parents=True)
    (folder / "format.csv.yaml").write_text(text.replace("ING Umsatzanzeige", "My Bank"), "utf-8")
    (folder / "plugin.yaml").write_text(
        "name: my_bank_csv\nversion: '1'\napi: 2\nformats: [format.csv.yaml]\n", "utf-8"
    )
    reg = plugins.discover(tmp_path / "plugins")
    assert "My Bank" not in [f.name for f in reg.formats()]
    assert "delimeter" in dict(reg.problems())["my_bank_csv"]
    assert "ING Umsatzanzeige" in [f.name for f in reg.formats()]


def test_llm_reference_examples_are_the_shipped_formats():
    """docs/csv-formats.llm.md shows the built-in formats: they must not drift."""
    import re

    from ruamel.yaml import YAML

    doc = (REPO_ROOT / "docs" / "csv-formats.llm.md").read_text(encoding="utf-8")
    examples = doc.split("## 7. Worked examples")[1].split("## 8.")[0]
    blocks = re.findall(r"```yaml\n(.*?)```", examples, re.DOTALL)
    shown = [CsvFormat.model_validate(YAML().load(b)) for b in blocks]
    shipped = {f.name: f for f in formats()}
    assert {f.name for f in shown} == set(shipped)
    for f in shown:
        assert f == shipped[f.name]


def test_builtin_formats_load_and_detect_their_files(tmp_path, statement):
    ing = tmp_path / "Umsatzanzeige.csv"
    ing.write_text(ing_csv(statement.transactions, fx.OPENING_CENTS), encoding="latin-1")
    paypal = tmp_path / "paypal.CSV"
    paypal.write_text(paypal_csv(PAYPAL_ROWS), encoding="utf-8-sig")
    detected = {
        path.name: [f.name for f in formats() if CsvImporter(f).detect(path)]
        for path in (ing, paypal)
    }
    assert detected == {"Umsatzanzeige.csv": ["ING Umsatzanzeige"], "paypal.CSV": ["PayPal"]}


def test_ing_csv(statement):
    stmt = CsvImporter(fmt("ING Umsatzanzeige")).parse_text(
        ing_csv(statement.transactions, fx.OPENING_CENTS)
    )
    assert (stmt.account_iban, stmt.account_name) == ("DE12345678901234567890", "ING Girokonto")
    assert stmt.balance_ok and stmt.warnings == []
    # oldest first, as in a Kontoauszug
    assert [t.booking_date for t in stmt.transactions] == [
        t.booking_date for t in statement.transactions
    ]
    assert (stmt.opening_balance_cents, stmt.closing_balance_cents) == (
        fx.OPENING_CENTS,
        fx.CLOSING_CENTS,
    )
    assert stmt.transactions[0].booking_type == "Dauerauftrag/Terminueberw."  # renamed
    vinted = next(t for t in stmt.transactions if "VINTED" in t.counterparty)
    assert vinted.merchant == statement.transactions[2].merchant  # enrich: ING


def test_ing_csv_after_the_pdf_brings_only_duplicates(app, statement_pdf, statement, tmp_path):
    """The same bookings from the PDF and from the CSV have the same fingerprint."""
    path = tmp_path / "Umsatzanzeige.csv"
    path.write_text(ing_csv(statement.transactions, fx.OPENING_CENTS), encoding="latin-1")
    with app.session() as s:
        import_service.import_file(s, app.settings, statement_pdf)
        p = import_service.preview(s, path, settings=app.settings)
    assert (p.new_count, p.duplicate_count) == (0, fx.BOOKING_COUNT)


# -- PayPal ---------------------------------------------------------------------------------

PAYPAL_HEADER = [
    "Datum",
    "Uhrzeit",
    "Name",
    "Typ",
    "Status",
    "Währung",
    "Netto",
    "Absender E-Mail-Adresse",
    "Transaktionscode",
    "Artikelbezeichnung",
    "Zugehöriger Transaktionscode",
    "Guthaben",
    "Betreff",
    "Hinweis",
    "Auswirkung auf Guthaben",
]
CARD = "Allgemeine Gutschrift auf Kreditkarte"
# date, name, type, status, cents, code, article, linked code (oldest first)
PAYPAL_ROWS = [
    # two equal purchases, funded by the debit card (the Giro shows them on 03.03.)
    ("01.03.2026", "Vinted UAB", "PayPal Express-Zahlung", "", -909, "A1", "Kinderjacke", ""),
    ("01.03.2026", "", CARD, "", 909, "F1", "", "A1"),
    ("02.03.2026", "Vinted UAB", "PayPal Express-Zahlung", "", -909, "A2", "Schuhe", ""),
    ("02.03.2026", "", CARD, "", 909, "F2", "", "A2"),
    # funded by direct debit, still pending (the Giro shows it on 05.03.)
    ("04.03.2026", "Shop GmbH", "Handyzahlung", "", -3250, "A3", "Lampe", ""),
    ("04.03.2026", "", "Bankgutschrift auf PayPal-Konto", "Ausstehend", 3250, "F3", "", "A3"),
    # money received, then paid from the PayPal balance
    ("06.03.2026", "Oma", "Allgemeine Zahlung", "", 5000, "B1", "", ""),
    ("07.03.2026", "Buchladen", "PayPal Express-Zahlung", "", -2000, "B2", "Roman", ""),
    # partly from the balance, partly from the card
    ("08.03.2026", "Teilzahlung AG", "PayPal Express-Zahlung", "", -4000, "C1", "Tisch", ""),
    ("08.03.2026", "", CARD, "", 1000, "F4", "", "C1"),
    # pending money received: left out until it is completed
    ("10.03.2026", "Freund", "Allgemeine Zahlung", "Ausstehend", 500, "P1", "", ""),
    # bought with the card, refunded to the card
    ("12.03.2026", "Retour GmbH", "PayPal Express-Zahlung", "", -1500, "R1", "Kleid", ""),
    ("12.03.2026", "", CARD, "", 1500, "F5", "", "R1"),
    ("14.03.2026", "Retour GmbH", "Rückzahlung", "", 1500, "R2", "", "R1"),
    ("14.03.2026", "", "Allgemeine Abbuchung von Kreditkarte", "", -1500, "F6", "", "R1"),
]


def paypal_csv(rows) -> str:
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    w.writerow(PAYPAL_HEADER)
    balance = 0
    for day, name, typ, status, cents, code, article, linked in rows:
        balance += cents
        sender = EMAIL if cents < 0 else f"{name.lower() or 'paypal'}@example.com"
        w.writerow(
            [
                day,
                "12:00:00",
                name,
                typ,
                status or "Abgeschlossen",
                "EUR",
                de(cents),
                sender,
                code,
                article,
                linked,
                de(balance),
                "",
                "",
                "Soll" if cents < 0 else "Haben",
            ]
        )
    return out.getvalue()


def paypal_account() -> OwnAccount:
    return OwnAccount(name="PayPal", kind="wallet", email=EMAIL)


def test_paypal_csv():
    stmt = CsvImporter(fmt("PayPal"), [paypal_account()]).parse_text(paypal_csv(PAYPAL_ROWS))
    assert (stmt.account_name, stmt.account_kind, stmt.dedupe_scope) == (
        "PayPal",
        "wallet",
        f"email:{EMAIL}",
    )
    # imported: money received, paid from the balance, the rest of the partly funded payment
    assert [(t.counterparty, t.amount_cents, t.external_id) for t in stmt.transactions] == [
        ("Oma", 5000, "B1"),
        ("Buchladen", -2000, "B2"),
        ("Teilzahlung AG", -3000, "C1"),
    ]
    assert [(lk.external_id, lk.amount_cents, lk.payee, lk.description) for lk in stmt.links] == [
        ("F1", -909, "Vinted UAB", "Kinderjacke"),
        ("F2", -909, "Vinted UAB", "Schuhe"),
        ("F3", -3250, "Shop GmbH", "Lampe"),  # pending funding still links the payment
        ("F4", -1000, "Teilzahlung AG", "Tisch"),
        ("F5", -1500, "Retour GmbH", "Kleid"),
        ("F6", 1500, "Retour GmbH", "Kleid"),  # the refund to the card
    ]
    # the pending money received (5 EUR) is in the PayPal balance, not in the bookings
    assert (stmt.opening_balance_cents, stmt.closing_balance_cents) == (0, 0)
    assert stmt.balance_ok and stmt.warnings == []


def test_paypal_account_not_configured():
    with pytest.raises(ImportError_, match=f"email: {EMAIL}"):
        CsvImporter(fmt("PayPal"), []).parse_text(paypal_csv(PAYPAL_ROWS))


def with_paypal(app):
    app.settings.app.own_accounts.append(paypal_account())
    return app.settings


def write(path: Path, rows) -> Path:
    path.write_text(paypal_csv(rows), encoding="utf-8-sig")
    return path


def giro_paypal_bookings(s) -> list[Transaction]:
    return list(
        s.scalars(
            select(Transaction)
            .join(Account, Transaction.account_id == Account.id)
            .where(Account.kind == "giro", Transaction.counterparty.ilike("%paypal%"))
            .order_by(Transaction.booking_date, Transaction.id)
        )
    )


def test_paypal_payments_link_the_giro_bookings(app, statement_pdf, tmp_path):
    settings = with_paypal(app)
    paypal = write(tmp_path / "paypal.csv", PAYPAL_ROWS)
    with app.session() as s:
        import_service.import_file(s, settings, statement_pdf)
        p = import_service.preview(s, paypal, settings=settings)
        assert (p.new_count, len(p.links_new), p.errors) == (3, 6, [])
        r = import_service.commit(s, settings, p, retrain=False)
    assert (r.added, r.links) == (3, 6)
    assert (r.linking.linked, r.linking.waiting) == (3, 3)  # no Giro bookings for the rest
    with app.session() as s:
        assert [(t.amount_cents, t.details) for t in giro_paypal_bookings(s)] == [
            (-909, "Vinted UAB · Kinderjacke"),
            (-909, "Vinted UAB · Schuhe"),
            (-3250, "Shop GmbH · Lampe"),
        ]
        wallet = s.scalar(select(Account).where(Account.name == "PayPal"))
        assert wallet.kind == "wallet" and wallet.on_budget
        total = s.scalar(
            select(func.sum(Transaction.amount_cents)).where(Transaction.account_id == wallet.id)
        )
        assert total == 0  # as the PayPal balance without the pending row

    # a later export overlapping this one: only what is new
    later = [*PAYPAL_ROWS, ("20.03.2026", "Oma", "Allgemeine Zahlung", "", 700, "B3", "", "")]
    with app.session() as s:
        p = import_service.preview(s, write(tmp_path / "paypal2.csv", later), settings=settings)
    assert (p.new_count, p.duplicate_count, len(p.links_new), p.links_known) == (1, 3, 0, 6)


def test_paypal_before_the_giro_waits_for_its_bank_booking(app, statement_pdf, tmp_path):
    settings = with_paypal(app)
    with app.session() as s:
        r = import_service.import_file(s, settings, write(tmp_path / "paypal.csv", PAYPAL_ROWS))
    assert (r.linking.linked, r.linking.waiting) == (0, 6)
    with app.session() as s:
        r = import_service.import_file(s, settings, statement_pdf)
    assert (r.linking.linked, r.linking.waiting) == (3, 3)
    with app.session() as s:
        assert all(t.details for t in giro_paypal_bookings(s))


def test_deleted_bank_booking_waits_again_and_undo_clears_details(app, statement_pdf, tmp_path):
    settings = with_paypal(app)
    with app.session() as s:
        giro = import_service.import_file(s, settings, statement_pdf)
        pp = import_service.import_file(s, settings, write(tmp_path / "paypal.csv", PAYPAL_ROWS))
    with app.session() as s:  # undo the Giro import: its bookings are gone
        import_service.delete_batch(s, settings, giro.batch_id, retrain=False)
    with app.session() as s:
        waiting = s.scalar(
            select(func.count())
            .select_from(LinkedPayment)
            .where(LinkedPayment.transaction_id.is_(None))
        )
        assert waiting == 6
        r = import_service.import_file(s, settings, statement_pdf)
    assert r.linking.linked == 3
    with app.session() as s:  # undo the PayPal import: the Giro bookings lose the details
        import_service.delete_batch(s, settings, pp.batch_id, retrain=False)
    with app.session() as s:
        assert [t.details for t in giro_paypal_bookings(s)] == [None, None, None]
        assert s.scalar(select(func.count()).select_from(LinkedPayment)) == 0


def test_rules_match_the_payment_details(app, statement_pdf, tmp_path):
    settings = with_paypal(app)
    category = settings.categories.full_keys()[0]
    rule = RuleDef(id="vinted", category=category, match=RuleMatch(details="(?i)^vinted uab"))
    settings.rules.rules.insert(0, rule)
    with app.session() as s:
        import_service.import_file(s, settings, statement_pdf)
        import_service.import_file(s, settings, write(tmp_path / "paypal.csv", PAYPAL_ROWS))
    with app.session() as s:
        vinted = [t for t in giro_paypal_bookings(s) if t.amount_cents == -909]
        assert [(sp.source, sp.rule_id) for t in vinted for sp in t.splits] == [
            (SplitSource.RULE, "vinted"),
            (SplitSource.RULE, "vinted"),
        ]


def test_import_dialog_reports_linked_and_waiting_payments(
    qtbot, app, statement_pdf, tmp_path, monkeypatch
):
    from PySide6.QtWidgets import QMessageBox

    from hiro_budget.gui.import_dialog import ImportDialog

    settings = with_paypal(app)
    with app.session() as s:
        import_service.import_file(s, settings, statement_pdf)
    shown: list[str] = []
    monkeypatch.setattr(QMessageBox, "information", lambda _p, _t, text: shown.append(text))
    monkeypatch.setattr(QMessageBox, "warning", lambda _p, _t, text: shown.append(text))
    monkeypatch.setattr(QMessageBox, "question", lambda *a: QMessageBox.StandardButton.Yes)
    dlg = ImportDialog(app, paths=[write(tmp_path / "paypal.csv", PAYPAL_ROWS)])
    qtbot.addWidget(dlg)
    assert "6 payment(s) via bank" in dlg.files.topLevelItem(0).text(7)
    assert dlg.import_button.isEnabled()
    dlg._import()
    assert "3 payment(s) linked to their bank booking." in shown[0]
    assert "3 payment(s) wait for their bank booking" in shown[0]


def test_account_email_survives_the_accounts_editor(app):
    from hiro_budget.config import load_settings
    from hiro_budget.gui.accounts_view import AccountDraft
    from hiro_budget.services import config_edit

    accounts = [*app.settings.app.own_accounts, paypal_account()]
    drafts = [AccountDraft.from_config(a) for a in accounts]
    config_edit.save_accounts(app.settings, [d.to_config() for d in drafts])
    saved = load_settings(app.settings.config_dir).app.own_accounts
    assert [(a.name, a.kind, a.email) for a in saved if a.email] == [("PayPal", "wallet", EMAIL)]


def test_csv_files_are_offered_for_import(app, tmp_path):
    folder = tmp_path / "input"
    folder.mkdir()
    write(folder / "paypal.CSV", PAYPAL_ROWS)
    (folder / "notes.txt").write_text("x", encoding="utf-8")
    with app.session() as s:
        assert [p.name for p in import_service.new_statement_files(s, folder)] == ["paypal.CSV"]


# -- Revolut ------------------------------------------------------------------------------

REVOLUT_HEADER = [
    "Type",
    "Product",
    "Started Date",
    "Completed Date",
    "Description",
    "Amount",
    "Fee",
    "Currency",
    "State",
    "Balance",
]
REVOLUT_TYPES = {
    "Kartenzahlung": "Card Payment",
    "Kartengutschrift": "Card Refund",
    "Einzahlung": "Deposit",
    "Überweisung": "Transfer",
    "Gutschrift": "Transfer",
}


def revolut_csv(transactions, opening_cents: int, extra=()) -> str:
    """A Revolut CSV export: ``transactions`` (parsed), then ``extra`` rows (type, day,
    description, amount, fee, state); rows not completed have no balance, as in the export."""
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    w.writerow(REVOLUT_HEADER)
    balance = opening_cents
    rows = [
        (
            REVOLUT_TYPES[t.booking_type],
            t.booking_date,
            t.merchant or t.counterparty,
            t.amount_cents,
            0,
            "COMPLETED",
        )
        for t in transactions
    ]
    for typ, day, description, cents, fee, state in [*rows, *extra]:
        completed = state == "COMPLETED"
        if completed:
            balance += cents - fee
        w.writerow(
            [
                typ,
                "Current",
                f"{day:%Y-%m-%d} 09:00:00",
                f"{day:%Y-%m-%d} 10:00:00" if completed else "",
                description,
                f"{cents / 100:.2f}",
                f"{fee / 100:.2f}",
                "EUR",
                state,
                f"{balance / 100:.2f}" if completed else "",
            ]
        )
    return out.getvalue()


MIGRATION = "Balance migration to another region or legal entity"


def test_revolut_csv():
    from datetime import date

    extra = [
        ("Card Payment", date(2026, 10, 1), "Cloud Storage Ltd", -744, 7, "COMPLETED"),
        ("Card Payment", date(2026, 10, 2), "Shop", -1000, 0, "REVERTED"),
        ("Transfer", date(2026, 10, 3), MIGRATION, -5000, 0, "COMPLETED"),
        ("Transfer", date(2026, 10, 3), MIGRATION, 5000, 0, "COMPLETED"),
        ("Card Payment", date(2026, 10, 4), "Bakery", -350, 0, "PENDING"),
    ]
    stmt = CsvImporter(fmt("Revolut")).parse_text(revolut_csv([], 10000, extra))
    assert [(t.booking_type, t.merchant, t.amount_cents) for t in stmt.transactions] == [
        ("Kartenzahlung", "Cloud Storage Ltd", -751),  # the fee is part of the booking
    ]
    assert stmt.transactions[0].value_date == date(2026, 10, 1)
    assert (stmt.account_name, stmt.match_by_date_amount) == ("Revolut", True)
    assert (stmt.opening_balance_cents, stmt.closing_balance_cents) == (10000, 10000 - 751)
    assert stmt.balance_ok and stmt.warnings == []


def test_revolut_csv_after_the_pdf_brings_only_new_bookings(app, tmp_path, monkeypatch):
    """The PDF and the CSV describe the same bookings with different texts: they are
    recognised by date and amount."""
    from datetime import date

    from hiro_budget.plugins.revolut_pdf import importer as revolut_pdf
    from tests.fixtures import revolut as rfx

    pdf = tmp_path / "account-statement.pdf"
    pdf.write_bytes(b"%PDF-")
    monkeypatch.setattr(revolut_pdf, "extract_pages", lambda _path: rfx.PAGES)
    monkeypatch.setattr(
        revolut_pdf.RevolutPdfImporter, "detect", lambda self, path: path.suffix == ".pdf"
    )
    parsed = revolut_pdf.parse_pages(rfx.PAGES)
    later = ("Card Payment", date(2026, 12, 1), "Cloud Storage Ltd", -744, 7, "COMPLETED")
    path = tmp_path / "account-statement.csv"
    path.write_text(
        revolut_csv(parsed.transactions, parsed.opening_balance_cents, [later]), encoding="utf-8"
    )
    with app.session() as s:
        import_service.import_file(s, app.settings, pdf)
        p = import_service.preview(s, path, settings=app.settings)
        assert (p.new_count, p.duplicate_count, p.errors) == (1, rfx.BOOKING_COUNT, [])
        r = import_service.commit(s, app.settings, p, mode=OverlapMode.ADD, retrain=False)
    assert r.added == 1
    with app.session() as s:
        count = s.scalar(select(func.count()).select_from(Transaction))
    assert count == rfx.BOOKING_COUNT + 1
