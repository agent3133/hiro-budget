"""ING Umsatzanzeige (booking list from online banking)."""

from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import func, select

from hiro_budget.db.models import Account, Transaction
from hiro_budget.importers.base import ImportError_, find_importer
from hiro_budget.plugins.ing_umsatzanzeige_pdf import importer as ums
from hiro_budget.services import import_service
from tests.conftest import write_statement_pdf
from tests.fixtures import ing_umsatzanzeige as fx


@pytest.fixture
def stmt():
    return ums.parse_pages(fx.PAGES)


def test_header_and_balance(stmt) -> None:
    assert stmt.warnings == []
    assert stmt.balance_ok
    assert (stmt.account_iban, stmt.account_kind, stmt.account_name) == (
        fx.IBAN,
        "giro",
        "ING Girokonto",
    )
    assert stmt.statement_date == date(2026, 10, 21)
    assert (stmt.period_start, stmt.period_end) == (date(2026, 10, 1), date(2026, 10, 21))
    # derived from the oldest booking and its balance
    assert (stmt.opening_balance_cents, stmt.closing_balance_cents) == (
        fx.OPENING_CENTS,
        fx.CLOSING_CENTS,
    )


def test_bookings_oldest_first(stmt) -> None:
    assert len(stmt.transactions) == fx.BOOKING_COUNT
    days = [t.booking_date for t in stmt.transactions]
    assert days == sorted(days)
    assert [t.amount_cents for t in stmt.transactions] == [
        -991_42,
        -1_49,
        7_20,
        -17_50,
        30_00,
        -18_84,
    ]


def test_booking_types_as_in_the_kontoauszug(stmt) -> None:
    assert [t.booking_type for t in stmt.transactions] == [
        "Dauerauftrag/Terminueberw.",
        "Entgelt",
        "Gutschrift/Dauerauftrag",
        "Lastschrift",
        "Gutschrift",
        "Lastschrift",
    ]


def test_card_payment(stmt) -> None:
    card = stmt.transactions[-1]
    assert (card.counterparty, card.merchant) == ("VISA REWE MUSTERSTADT", "REWE MUSTERSTADT")
    assert card.card_purchase_date == date(2026, 10, 19)
    assert card.value_date == date(2026, 10, 21)
    assert card.purpose.endswith("Google Pay")


def test_purpose_that_looks_like_a_page_number(stmt) -> None:
    assert stmt.transactions[3].purpose == "26/04008"


def test_amount_not_matching_the_balance_is_reported() -> None:
    page = fx.PAGE_1.replace("+30,00 €", "+31,00 €")
    stmt = ums.parse_pages([page, fx.PAGE_2])
    assert any("does not match the balance change" in w for w in stmt.warnings)


def test_missing_booking_is_reported() -> None:
    page = fx.PAGE_1.replace("absteigend 6", "absteigend 7")
    stmt = ums.parse_pages([page, fx.PAGE_2])
    assert any("the header lists 7" in w for w in stmt.warnings)


def test_ascending_order() -> None:
    lines = fx.PAGE_2.splitlines()
    start = lines.index("Notiz") + 1
    bookings = [lines[i : i + 3] for i in range(start, len(lines) - 1, 3)]
    body = "\n".join(line for b in reversed(bookings) for line in b)
    page = "\n".join(lines[:start]) + "\n" + body + "\n1/1\n"
    page = page.replace(
        "Kontoname Girokonto",
        "Kontoname Girokonto\n01.10.2026 bis 05.10.2026 - / Datum aufsteigend 3",
    )
    stmt = ums.parse_pages([page.replace("Bank ING", "Bank ING\nDE12 3456 7890 1234 5678 90")])
    assert stmt.warnings == [] and stmt.balance_ok
    assert [t.amount_cents for t in stmt.transactions] == [-991_42, -1_49, 7_20]


def test_without_bookings() -> None:
    with pytest.raises(ImportError_, match="no bookings"):
        ums.parse_pages([fx.PAGE_1.split("Notiz")[0]])


def test_detected_as_umsatzanzeige(tmp_path: Path) -> None:
    # The PDF test font has no euro sign; detection only needs the header.
    pdf = write_statement_pdf(
        tmp_path / "Umsatzanzeige.pdf", [p.replace("€", "") for p in fx.PAGES]
    )
    assert find_importer(pdf).name == ums.IngUmsatzanzeigePdfImporter.name


def test_import_into_configured_account(app, tmp_path: Path, monkeypatch) -> None:
    pdf = tmp_path / "Umsatzanzeige.pdf"
    pdf.write_bytes(b"%PDF-")
    monkeypatch.setattr(ums, "extract_pages", lambda _path: fx.PAGES)
    monkeypatch.setattr(ums.IngUmsatzanzeigePdfImporter, "detect", lambda self, path: True)
    with app.session() as s:
        p = import_service.preview(s, pdf)
        assert p.errors == [] and p.can_commit and p.account_exists
        assert p.account_name == "ING Girokonto"
        import_service.commit(s, app.settings, p)
    with app.session() as s:
        giro = s.scalar(select(Account).where(Account.name == "ING Girokonto"))
        count = s.scalar(
            select(func.count()).select_from(Transaction).where(Transaction.account_id == giro.id)
        )
        assert count == fx.BOOKING_COUNT


def test_savings_layout_without_payee_and_note_header() -> None:
    # Extra-Konto export: no "Notiz" header line, securities bookings without payee
    pages = [
        p.replace("Notiz\n", "")
        .replace("02.10.2026 ING 1.007,09 €", "02.10.2026 1.007,09 €")
        .replace("02.10.2026 Entgelt", "02.10.2026 Zins / Dividende WP")
        for p in fx.PAGES
    ]
    stmt = ums.parse_pages(pages)
    assert stmt.warnings == [] and stmt.balance_ok
    assert len(stmt.transactions) == fx.BOOKING_COUNT
    t = stmt.transactions[1]
    assert (t.counterparty, t.booking_type, t.amount_cents) == ("", "Zins/Dividende WP", -1_49)
