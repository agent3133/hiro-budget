from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import func, select

from hiro_budget.db.models import Account, ImportBatch, Transaction
from hiro_budget.importers.base import ParsedTransaction
from hiro_budget.services import import_service
from hiro_budget.services.import_service import DuplicateFileError, dedupe_keys
from tests.conftest import write_statement_pdf
from tests.fixtures import ing_statement as fx


def count(session, model) -> int:
    return session.scalar(select(func.count()).select_from(model))


def count_imported(session) -> int:
    """Imported bookings (excludes automatic transfer counter-bookings)."""
    return session.scalar(
        select(func.count()).select_from(Transaction).where(Transaction.batch_id.is_not(None))
    )


def test_dedupe_keys_distinguish_identical_bookings() -> None:
    t = ParsedTransaction(date(2026, 3, 3), -909, "Lastschrift", "VISA VINTED", "x")
    keys = dedupe_keys("DE1", [t, t])
    assert keys[0] != keys[1]
    assert dedupe_keys("DE1", [t, t]) == keys  # stable


def test_dedupe_keys_ignore_whitespace_differences() -> None:
    a = ParsedTransaction(date(2026, 3, 3), -500, "Lastschrift", "PayPal", "Ihr Ei nkauf bei")
    b = ParsedTransaction(date(2026, 3, 3), -500, "Lastschrift", "PayPal", "Ihr Einkauf bei")
    assert dedupe_keys("DE1", [a]) == dedupe_keys("DE1", [b])


def test_import_end_to_end(app, statement_pdf: Path) -> None:
    with app.session() as s:
        p = import_service.preview(s, statement_pdf)
        assert p.can_commit and p.new_count == fx.BOOKING_COUNT
        assert p.account_exists and p.account_name == "ING Girokonto"
        result = import_service.commit(s, app.settings, p)
        assert result.added == fx.BOOKING_COUNT

    with app.session() as s:
        assert count_imported(s) == fx.BOOKING_COUNT
        batch = s.scalar(select(ImportBatch))
        assert batch.account.name == "ING Girokonto"
        assert batch.closing_balance_cents == fx.CLOSING_CENTS
        # No extra account created: matched via IBAN from config.
        assert count(s, Account) == 4


def test_same_file_cannot_be_imported_twice(app, statement_pdf: Path) -> None:
    with app.session() as s:
        import_service.import_file(s, app.settings, statement_pdf)
    with app.session() as s:
        p = import_service.preview(s, statement_pdf)
        assert p.file_already_imported and not p.can_commit
        assert p.duplicate_count == fx.BOOKING_COUNT
        with pytest.raises(DuplicateFileError):
            import_service.commit(s, app.settings, p)


def test_balance_mismatch_blocks_commit(app, tmp_path: Path) -> None:
    bad = write_statement_pdf(
        tmp_path / "bad.pdf", [fx.PAGE_1, fx.PAGE_2.replace("4.000,00", "4.100,00")]
    )
    with app.session() as s:
        p = import_service.preview(s, bad)
        assert not p.can_commit
        assert any("balance" in e for e in p.errors)
        with pytest.raises(import_service.BalanceError):
            import_service.commit(s, app.settings, p)


def test_unknown_iban_creates_account(app, tmp_path: Path) -> None:
    pages = [
        p.replace("DE12 3456 7890 1234 5678 90", "DE99 9999 9999 9999 9999 99") for p in fx.PAGES
    ]
    pdf = write_statement_pdf(tmp_path / "x.pdf", pages)
    with app.session() as s:
        import_service.import_file(s, app.settings, pdf)
    with app.session() as s:
        acc = s.scalar(select(Account).where(Account.iban == "DE99999999999999999999"))
        assert acc.name == "ING Girokonto 1234567890"
