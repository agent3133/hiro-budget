"""The batch import behind the import dialog and the ``import`` command (#76), no Qt."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from sqlalchemy import select

from hiro_budget.db.models import Account
from hiro_budget.services import connections, import_batch, import_service
from hiro_budget.services.import_service import OverlapMode
from tests.conftest import write_statement_pdf
from tests.fixtures import ing_statement as fx
from tests.test_bank_fetch import IBAN
from tests.test_overlap import EXTRA, count_imported


def variant(tmp_path: Path, name: str, extra: str = EXTRA, saldo: str = "3.545,87") -> Path:
    """The fixture statement plus one booking (as test_overlap.extended_pdf)."""
    page2 = fx.PAGE_2.replace("Neuer Saldo 3.555,87\n", extra + f"Neuer Saldo {saldo}\n")
    page1 = fx.PAGE_1.replace("Neuer Saldo 3.555,87", f"Neuer Saldo {saldo}")
    return write_statement_pdf(tmp_path / name, [page1, page2])


def never(p):
    raise AssertionError(f"asked for {p.path.name}")


def statuses(batch) -> list[str]:
    return [f.status for f in batch.files]


def test_a_batch_tallies_and_skips(app, statement_pdf):
    batch = import_batch.import_files(app.session, app.settings, [statement_pdf] * 2, never)
    assert statuses(batch) == ["imported", "already_imported"]
    assert (batch.added, batch.duplicates) == (fx.BOOKING_COUNT, fx.BOOKING_COUNT)
    assert batch.imported_any and batch.errors == []
    assert batch.backup is not None and batch.backup.is_dir()  # backup.before_import


def test_overlap_is_asked_and_apply_to_all_is_kept(app, statement_pdf, tmp_path):
    with app.session() as s:
        import_service.import_file(s, app.settings, statement_pdf)
    first = variant(tmp_path, "a.pdf")
    second = variant(
        tmp_path,
        "b.pdf",
        "28.03.2026 Lastschrift VISA EDEKA NEU -5,00\n28.03.2026 KAUFUMSATZ 27.03\n",
        "3.550,87",
    )
    asked = []

    def decide(p):
        asked.append(p.path.name)
        return OverlapMode.ADD, True  # "apply to all further overlapping files"

    batch = import_batch.import_files(app.session, app.settings, [first, second], decide)
    assert asked == ["a.pdf"] and statuses(batch) == ["imported", "imported"]
    assert [f.mode for f in batch.files] == [OverlapMode.ADD] * 2
    assert batch.added == 2
    with app.session() as s:
        assert count_imported(s) == fx.BOOKING_COUNT + 2


def test_cancel_without_apply_to_all_asks_again(app, statement_pdf, tmp_path):
    with app.session() as s:
        import_service.import_file(s, app.settings, statement_pdf)
    files = [variant(tmp_path, "a.pdf"), variant(tmp_path, "b.pdf")]
    asked = []
    batch = import_batch.import_files(
        app.session, app.settings, files, lambda p: asked.append(p) or (None, False)
    )
    assert len(asked) == 2 and statuses(batch) == ["cancelled", "cancelled"]
    assert not batch.imported_any


def test_a_failing_file_does_not_stop_the_others(app, statement_pdf, tmp_path):
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"%PDF-1.4 not really")
    batch = import_batch.import_files(app.session, app.settings, [broken, statement_pdf], never)
    assert statuses(batch) == ["failed", "imported"]
    assert batch.errors[0].startswith("broken.pdf: ")
    assert batch.added == fx.BOOKING_COUNT


def test_a_statement_covered_by_fints_is_recorded(app, statement_pdf, monkeypatch):
    """The drift the CLI had: everything fetched already -> recorded, not offered again."""
    with app.session() as s:
        account_id = s.scalar(select(Account.id).where(Account.iban == IBAN))
    monkeypatch.setattr(
        connections,
        "cutovers",
        lambda s, settings: {account_id: (date(2026, 3, 1), date(2026, 3, 31))},
    )
    batch = import_batch.import_files(app.session, app.settings, [statement_pdf], never)
    assert statuses(batch) == ["imported"] and batch.added == 0
    with app.session() as s:
        assert import_service.new_statement_files(s, statement_pdf.parent) == []


def test_a_failed_backup_asks_and_can_stop(app, statement_pdf, monkeypatch):
    from hiro_budget.services import backup

    def fail(*a):
        raise OSError("disk full")

    monkeypatch.setattr(backup, "create_backup", fail)
    seen = []
    batch = import_batch.import_files(
        app.session,
        app.settings,
        [statement_pdf],
        never,
        backup_failed=lambda e: seen.append(str(e)) or False,
    )
    assert seen == ["disk full"] and batch.stopped and batch.files == []
    batch = import_batch.import_files(app.session, app.settings, [statement_pdf], never)
    assert batch.backup_error == "disk full" and statuses(batch) == ["imported"]
