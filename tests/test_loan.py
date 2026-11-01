"""ING Baufinanzierung statements: imported as balance statements of the loan account."""

from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import func, select

from hiro_budget.db.models import Account, Transaction
from hiro_budget.importers.base import find_importer
from hiro_budget.plugins.ing_loan_pdf import importer as ing_loan_pdf
from hiro_budget.services import accounts as acc_service
from hiro_budget.services import import_service
from hiro_budget.services.counterparts import new_manual_key
from tests.conftest import TEST_CONFIG, write_statement_pdf
from tests.fixtures import ing_loan as fx

LOAN_CONFIG = TEST_CONFIG.replace(
    "  - name: ING Baufinanzierung\n    kind: loan\n",
    f"  - name: ING Baufinanzierung\n    kind: loan\n    iban: {fx.IBAN}\n",
)


@pytest.fixture
def app(config_dir: Path):
    from hiro_budget.app import App

    (config_dir / "config.yaml").write_text(LOAN_CONFIG, encoding="utf-8")
    return App.from_config_dir(config_dir)


@pytest.fixture
def loan_pdf(tmp_path: Path) -> Path:
    return write_statement_pdf(tmp_path / "Direkt_Baufinanzierung.pdf", fx.PAGES)


def loan_balance(s) -> acc_service.AccountBalance:
    return next(b for b in acc_service.account_balances(s) if b.name == "ING Baufinanzierung")


def test_parse_loan_statement() -> None:
    stmt = ing_loan_pdf.parse_pages(fx.PAGES)
    assert stmt.warnings == []
    assert stmt.valuation and stmt.balance_ok and stmt.holdings == []
    assert stmt.account_iban == fx.IBAN
    assert stmt.account_kind == "loan"
    assert (stmt.period_start, stmt.period_end) == (date(2025, 1, 1), date(2025, 12, 31))
    assert stmt.statement_date == date(2025, 12, 30)
    assert (stmt.opening_balance_cents, stmt.closing_balance_cents) == (
        -200_000_00,
        fx.CLOSING_CENTS,
    )
    assert len(stmt.transactions) == fx.BOOKING_COUNT
    payment = stmt.transactions[-1]
    assert (payment.booking_type, payment.counterparty, payment.amount_cents) == (
        "Lastschrifteinzug",
        "Musterfrau",
        1_510_00,
    )
    assert payment.purpose.endswith("TILGUNG 1.000,00")
    assert stmt.transactions[2].booking_type == "Auszahlung"


def test_balance_mismatch_is_reported() -> None:
    stmt = ing_loan_pdf.parse_pages(
        [fx.PAGE_1.replace("Zinsen -520,00", "Zinsen -521,00"), fx.PAGE_2]
    )
    assert not stmt.balance_ok
    assert any("balance mismatch" in w for w in stmt.warnings)


def test_detected_as_loan_statement(loan_pdf) -> None:
    assert find_importer(loan_pdf).name == ing_loan_pdf.IngLoanPdfImporter.name


def test_import_sets_loan_balance_without_bookings(app, loan_pdf) -> None:
    with app.session() as s:
        loan = s.scalar(select(Account).where(Account.name == "ING Baufinanzierung"))
        # principal counter-bookings from the Girokonto: before and after the statement
        for day, cents in ((date(2025, 12, 30), 1_000_00), (date(2026, 1, 30), 1_003_00)):
            s.add(
                Transaction(
                    account=loan,
                    booking_date=day,
                    amount_cents=cents,
                    booking_type="Umbuchung",
                    dedupe_key=new_manual_key(),
                )
            )
    with app.session() as s:
        p = import_service.preview(s, loan_pdf)
        assert p.can_commit and p.errors == [] and p.account_exists
        import_service.commit(s, app.settings, p)
    with app.session() as s:
        b = loan_balance(s)
        # statement balance + principal after the statement date
        assert b.balance_cents == fx.CLOSING_CENTS + 1_003_00
        assert (b.anchor, b.statement_date) == ("valuation", date(2025, 12, 31))
        assert acc_service.positions(s, b.account_id) is None  # no securities
        # the loan's own bookings are not stored (no double counting of the payments)
        assert s.scalar(select(func.count()).select_from(Transaction)) == 2
        [info] = import_service.list_batches(s)
        assert (info.valuation, info.holding_count, info.booking_count) == (True, 0, 0)
