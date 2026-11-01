"""Revolut account statements (German PDF export)."""

from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import func, select

from hiro_budget.db.models import Account, Transaction
from hiro_budget.importers.base import ImportError_, find_importer
from hiro_budget.plugins.revolut_pdf import importer as revolut_pdf
from hiro_budget.services import import_service
from tests.conftest import write_statement_pdf
from tests.fixtures import revolut as fx


@pytest.fixture
def stmt():
    return revolut_pdf.parse_pages(fx.PAGES)


def test_header_and_balance(stmt) -> None:
    assert stmt.warnings == []
    assert stmt.balance_ok
    assert (stmt.institution, stmt.account_kind, stmt.account_iban) == ("Revolut", "giro", fx.IBAN)
    assert stmt.statement_date == date(2026, 10, 21)
    assert (stmt.period_start, stmt.period_end) == (date(2021, 1, 16), date(2026, 10, 21))
    assert (stmt.opening_balance_cents, stmt.closing_balance_cents) == (0, fx.CLOSING_CENTS)
    assert not stmt.valuation


def test_move_between_entities_and_reversals_are_skipped(stmt) -> None:
    assert len(stmt.transactions) == fx.BOOKING_COUNT
    assert not any("Revolut" in t.counterparty for t in stmt.transactions)
    assert not any(t.merchant == "Brave Browser" for t in stmt.transactions)


def test_sign_from_running_balance(stmt) -> None:
    assert [t.amount_cents for t in stmt.transactions] == [
        40_00,
        -30_85,
        1_000_00,
        3_52,
        -130_49,
        -81_84,
        -744_18,
    ]


def test_card_payment_and_refund(stmt) -> None:
    card = stmt.transactions[1]
    assert (card.booking_type, card.merchant, card.counterparty) == (
        "Kartenzahlung",
        "Microsoft Store",
        "Microsoft*store, Msbill.info",
    )
    assert "4.479 ISK" in card.purpose and "Karte" not in card.purpose
    refund = stmt.transactions[3]
    assert (refund.booking_type, refund.merchant) == ("Kartengutschrift", "Mvideo")
    assert refund.purpose.endswith("302,00 RUB")
    fee = stmt.transactions[4]
    assert "Gebühr: 1,29€" in fee.purpose


def test_top_up_and_transfers(stmt) -> None:
    top_up = stmt.transactions[0]
    assert (top_up.booking_type, top_up.counterparty) == ("Einzahlung", "*1234")
    assert top_up.purpose == "Google Pay Einzahlung von *1234 Von: *5678"
    incoming = stmt.transactions[2]
    assert incoming.booking_type == "Gutschrift"
    # the wrapped name is complete in the "Von:" line; the IBAN stays in the purpose
    assert incoming.counterparty == "ERIKA MUSTERFRAU.MAX MUSTERMANN"
    assert "DE96500105171234567890" in incoming.purpose
    assert "Referenz: Urlaub" in incoming.purpose
    outgoing = stmt.transactions[5]
    assert (outgoing.booking_type, outgoing.counterparty) == ("Überweisung", "ERIKA MUSTERFRAU")


def test_balance_column_mismatch_is_reported() -> None:
    pages = [fx.PAGE_1.replace("30,85€ 9,15€", "30,85€ 9,25€"), fx.PAGE_2, fx.PAGE_3]
    stmt = revolut_pdf.parse_pages(pages)
    assert any("does not match the balance change" in w for w in stmt.warnings)


def test_summary_mismatch_is_reported() -> None:
    page = fx.PAGE_1.replace("0,00€ 1.043,52€ 1.043,52€", "0,00€ 1.043,52€ 1.043,62€", 1)
    stmt = revolut_pdf.parse_pages([page, fx.PAGE_2, fx.PAGE_3])
    assert any("do not match the summary" in w for w in stmt.warnings)


def test_other_currencies_are_rejected() -> None:
    with pytest.raises(ImportError_, match="only EUR"):
        revolut_pdf.parse_pages([fx.PAGE_1.replace("EUR-Kontoauszug", "USD-Kontoauszug")])


def test_detected_as_revolut_statement(tmp_path: Path) -> None:
    # The PDF test font has no euro sign; detection only needs the header.
    pages = [p.replace("€", "") for p in fx.PAGES]
    pdf = write_statement_pdf(tmp_path / "account-statement_de-de.pdf", pages)
    assert find_importer(pdf).name == revolut_pdf.RevolutPdfImporter.name


def test_import_creates_account(app, tmp_path: Path, monkeypatch) -> None:
    pdf = tmp_path / "account-statement.pdf"
    pdf.write_bytes(b"%PDF-")
    monkeypatch.setattr(revolut_pdf, "extract_pages", lambda _path: fx.PAGES)
    monkeypatch.setattr(revolut_pdf.RevolutPdfImporter, "detect", lambda self, path: True)
    with app.session() as s:
        p = import_service.preview(s, pdf)
        assert p.errors == [] and p.can_commit and not p.account_exists
        import_service.commit(s, app.settings, p)
    with app.session() as s:
        account = s.scalar(select(Account).where(Account.iban == fx.IBAN))
        assert account is not None and account.institution == "Revolut"
        count = s.scalar(select(func.count()).select_from(Transaction))
        assert count == fx.BOOKING_COUNT
