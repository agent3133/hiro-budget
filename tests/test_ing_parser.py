from datetime import date
from pathlib import Path

import pytest

from hiro_budget.importers.base import find_importer
from hiro_budget.plugins.ing_giro_pdf.importer import (
    IngGiroPdfImporter,
    parse_pages,
    split_type_and_counterparty,
)
from tests.fixtures import ing_statement as fx


@pytest.fixture
def stmt():
    return parse_pages(fx.PAGES)


def test_header(stmt) -> None:
    assert stmt.account_iban == "DE12345678901234567890"
    assert stmt.account_name == "ING Girokonto 1234567890"
    assert stmt.account_kind == "giro"
    assert stmt.statement_no == "3"
    assert stmt.statement_date == date(2026, 3, 31)
    assert (stmt.period_start, stmt.period_end) == (date(2026, 3, 1), date(2026, 3, 31))
    assert stmt.opening_balance_cents == fx.OPENING_CENTS
    assert stmt.closing_balance_cents == fx.CLOSING_CENTS


def test_bookings_and_balance(stmt) -> None:
    assert len(stmt.transactions) == fx.BOOKING_COUNT
    assert stmt.balance_ok
    assert stmt.warnings == []


def test_header_block_after_table_is_not_part_of_last_booking(stmt) -> None:
    # Page 1's header text follows the T_ marker and must not leak into a purpose.
    last_on_page_1 = stmt.transactions[3]
    assert "Saldo" not in last_on_page_1.purpose
    assert last_on_page_1.purpose.endswith("ARN33333333333333333333333")


def test_card_booking(stmt) -> None:
    t = stmt.transactions[1]
    assert t.booking_type == "Lastschrift"
    assert t.counterparty == "VISA REWE MUSTERSTADT"
    assert t.merchant == "REWE MUSTERSTADT"
    assert t.card_purchase_date == date(2026, 2, 28)
    assert t.value_date == date(2026, 3, 2)
    assert t.amount_cents == -2345


def test_identical_same_day_bookings_are_both_kept(stmt) -> None:
    vinted = [t for t in stmt.transactions if t.merchant == "VINTED"]
    assert len(vinted) == 2
    assert all(t.amount_cents == -909 for t in vinted)


def test_references_and_paypal_merchant(stmt) -> None:
    t = stmt.transactions[4]
    assert t.mandate_ref == "5M3J2256LF2BN"
    assert t.reference == "1052751695622"
    assert t.merchant == "PayPal: eBay S.a r.l."
    assert "Mandat" not in t.purpose


def test_wrapped_purpose_is_joined(stmt) -> None:
    mortgage = stmt.transactions[-1]
    assert mortgage.purpose.endswith("ZINSEN 600,00 TILGUNG 400,00")
    assert mortgage.amount_cents == -100000


def test_standing_order_with_reference_only(stmt) -> None:
    t = stmt.transactions[0]
    assert t.booking_type == "Dauerauftrag/Terminueberw."
    assert t.counterparty == "Hausverwaltung Muster"
    assert t.reference == "HV-2026-03"


def test_balance_mismatch_is_reported() -> None:
    pages = [fx.PAGE_1, fx.PAGE_2.replace("4.000,00", "4.000,01")]
    stmt = parse_pages(pages)
    assert not stmt.balance_ok
    assert stmt.balance_difference_cents == -1
    assert any("balance mismatch" in w for w in stmt.warnings)


def test_footnote_marker_after_amount() -> None:
    # A superscript footnote marker is extracted as a digit glued to the amount.
    pages = [
        fx.PAGE_1,
        fx.PAGE_2.replace("Lastschrift Musterfrau -1.000,00", "Lastschrift Musterfrau -1.000,001"),
    ]
    stmt = parse_pages(pages)
    assert stmt.balance_ok and stmt.warnings == []
    mortgage = stmt.transactions[-1]
    assert (mortgage.counterparty, mortgage.amount_cents) == ("Musterfrau", -1_000_00)
    assert "ZINSEN" in mortgage.purpose


def test_unknown_booking_type_warns() -> None:
    pages = [fx.PAGE_1.replace("Dauerauftrag/Terminueberw.", "Sonderbuchung"), fx.PAGE_2]
    stmt = parse_pages(pages)
    assert stmt.transactions[0].booking_type == "Sonderbuchung"
    assert any("unknown booking type" in w for w in stmt.warnings)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Gutschrift/Dauerauftrag Max", ("Gutschrift/Dauerauftrag", "Max", True)),
        ("Gutschrift Finanzamt", ("Gutschrift", "Finanzamt", True)),
        ("Entgelt", ("Entgelt", "", True)),
        ("Foo Bar Baz", ("Foo", "Bar Baz", False)),
    ],
)
def test_split_type(text, expected) -> None:
    assert split_type_and_counterparty(text) == expected


def test_generated_pdf_end_to_end(statement_pdf: Path) -> None:
    importer = find_importer(statement_pdf)
    assert importer.name == IngGiroPdfImporter.name
    stmt = importer.parse(statement_pdf)
    assert len(stmt.transactions) == fx.BOOKING_COUNT
    assert stmt.balance_ok
    assert stmt.transactions[6].booking_type == "Gehalt/Rente"


def test_detect_rejects_other_files(tmp_path: Path) -> None:
    other = tmp_path / "notes.pdf"
    other.write_bytes(b"not a pdf")
    assert not IngGiroPdfImporter().detect(other)


def test_real_statements_balance(real_statements: list[Path]) -> None:
    """Runs only locally when real statements exist in input/."""
    for path in real_statements:
        stmt = find_importer(path).parse(path)
        assert stmt.balance_ok, f"{path.name}: {stmt.warnings}"
