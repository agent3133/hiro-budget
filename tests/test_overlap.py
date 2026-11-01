"""Importing statements that overlap already stored bookings."""

from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import func, select

from hiro_budget.db.models import Category, ImportBatch, Transaction
from hiro_budget.services import import_service
from hiro_budget.services import transactions as txs
from hiro_budget.services.import_service import OverlapError, OverlapMode
from tests.conftest import write_statement_pdf
from tests.fixtures import ing_statement as fx

EXTRA = "28.03.2026 Lastschrift VISA REWE NEU -10,00\n28.03.2026 KAUFUMSATZ 27.03\n"


@pytest.fixture
def extended_pdf(tmp_path: Path) -> Path:
    """Same statement as the fixture plus one extra booking (REWE NEU, -10,00)."""
    page2 = fx.PAGE_2.replace("Neuer Saldo 3.555,87\n", EXTRA + "Neuer Saldo 3.545,87\n")
    page1 = fx.PAGE_1.replace("Neuer Saldo 3.555,87", "Neuer Saldo 3.545,87")
    return write_statement_pdf(tmp_path / "extended.pdf", [page1, page2])


def count(session, model) -> int:
    return session.scalar(select(func.count()).select_from(model))


def count_imported(session) -> int:
    """Imported bookings (excludes automatic transfer counter-bookings)."""
    return session.scalar(
        select(func.count()).select_from(Transaction).where(Transaction.batch_id.is_not(None))
    )


def cat_id(session, key: str) -> int:
    return session.scalar(select(Category.id).where(Category.key == key))


def test_overlap_is_detected(app, statement_pdf, extended_pdf) -> None:
    with app.session() as s:
        import_service.import_file(s, app.settings, statement_pdf)
    with app.session() as s:
        p = import_service.preview(s, extended_pdf)
    o = p.overlap
    assert o is not None
    assert (o.date_from, o.date_to) == (date(2026, 3, 1), date(2026, 3, 31))
    assert o.existing_count == fx.BOOKING_COUNT
    assert o.removed_on_overwrite == 0
    assert o.files == [statement_pdf.name]
    assert (p.new_count, p.duplicate_count) == (1, fx.BOOKING_COUNT)


def test_no_overlap_for_other_period(app, statement_pdf, tmp_path) -> None:
    may = [pg.replace(".03.2026", ".05.2026").replace("März", "Mai") for pg in fx.PAGES]
    may_pdf = write_statement_pdf(tmp_path / "may.pdf", may)
    with app.session() as s:
        import_service.import_file(s, app.settings, statement_pdf)
    with app.session() as s:
        p = import_service.preview(s, may_pdf)
        assert p.overlap is None
        import_service.commit(s, app.settings, p)  # no mode needed
    with app.session() as s:
        assert count_imported(s) == 2 * fx.BOOKING_COUNT


def test_mode_required_on_overlap(app, statement_pdf, extended_pdf) -> None:
    with app.session() as s:
        import_service.import_file(s, app.settings, statement_pdf)
    with app.session() as s:
        with pytest.raises(OverlapError):
            import_service.import_file(s, app.settings, extended_pdf)


def test_add_missing(app, statement_pdf, extended_pdf) -> None:
    with app.session() as s:
        import_service.import_file(s, app.settings, statement_pdf)
    with app.session() as s:
        r = import_service.import_file(s, app.settings, extended_pdf, OverlapMode.ADD)
        assert (r.added, r.duplicates, r.replaced) == (1, fx.BOOKING_COUNT, 0)
    with app.session() as s:
        assert count_imported(s) == fx.BOOKING_COUNT + 1
        assert count(s, ImportBatch) == 2


def test_overwrite_replaces_range_and_keeps_manual_work(app, statement_pdf, extended_pdf) -> None:
    # First the extended statement (9 bookings), with manual work on two bookings.
    with app.session() as s:
        import_service.import_file(s, app.settings, extended_pdf)
    with app.session() as s:
        handwerker = txs.list_transactions(s, txs.TxFilter(text="Handwerker"))[0]
        rewe_neu = txs.list_transactions(s, txs.TxFilter(text="REWE NEU"))[0]
        txs.set_category(s, handwerker.id, cat_id(s, "wohnen.instandhaltung"))
        txs.set_notes(s, handwerker.id, "Heizung")
        txs.set_category(s, rewe_neu.id, cat_id(s, "freizeit.restaurants"))

    # Overwrite with the original statement, which does not contain REWE NEU.
    with app.session() as s:
        p = import_service.preview(s, statement_pdf)
        o = p.overlap
        assert (o.existing_count, o.removed_on_overwrite) == (fx.BOOKING_COUNT + 1, 1)
        assert (o.manual_kept, o.manual_lost) == (1, 1)
        r = import_service.commit(s, app.settings, p, mode=OverlapMode.OVERWRITE)
        assert (r.added, r.duplicates, r.replaced) == (fx.BOOKING_COUNT, 0, fx.BOOKING_COUNT + 1)

    with app.session() as s:
        assert count_imported(s) == fx.BOOKING_COUNT
        assert txs.list_transactions(s, txs.TxFilter(text="REWE NEU")) == []
        row = txs.list_transactions(s, txs.TxFilter(text="Handwerker"))[0]
        assert row.is_manual and row.notes == "Heizung"
        assert row.splits[0].category_key == "wohnen.instandhaltung"
        # The replaced import became empty and left the history.
        batches = import_service.list_batches(s)
        assert [b.source_file for b in batches] == [statement_pdf.name]
        # Non-manual bookings were categorized again.
        rewe = txs.list_transactions(s, txs.TxFilter(text="REWE MUSTERSTADT"))[0]
        assert rewe.splits[0].category_key == "lebensmittel.supermarkt"


def test_overwrite_with_nothing_new_is_possible(app, statement_pdf, tmp_path) -> None:
    copy = tmp_path / "copy.pdf"
    copy.write_bytes(statement_pdf.read_bytes() + b"\n%")  # same content, different hash
    with app.session() as s:
        import_service.import_file(s, app.settings, statement_pdf)
    with app.session() as s:
        p = import_service.preview(s, copy)
        assert p.new_count == 0 and p.can_commit
        r = import_service.commit(s, app.settings, p, mode=OverlapMode.OVERWRITE)
        assert r.added == fx.BOOKING_COUNT
    with app.session() as s:
        assert count_imported(s) == fx.BOOKING_COUNT
        assert count(s, ImportBatch) == 1


# --- CLI ----------------------------------------------------------------------------


def test_cli_on_overlap(app, config_dir, statement_pdf, extended_pdf, capsys) -> None:
    from hiro_budget.__main__ import main

    base = ["--config-dir", str(config_dir), "import"]
    assert main([*base, str(statement_pdf)]) == 0
    # Non-interactive without a flag: skipped with a hint.
    assert main([*base, str(extended_pdf)]) == 0
    assert "use --on-overlap" in capsys.readouterr().out
    assert main([*base, str(extended_pdf), "--on-overlap", "cancel"]) == 0
    with app.session() as s:
        assert count_imported(s) == fx.BOOKING_COUNT
    assert main([*base, str(extended_pdf), "--on-overlap", "overwrite"]) == 0
    assert f"replaced {fx.BOOKING_COUNT}" in capsys.readouterr().out
    with app.session() as s:
        assert count_imported(s) == fx.BOOKING_COUNT + 1
        assert count(s, ImportBatch) == 1


# --- GUI ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("choice", "expected"),
    [
        (OverlapMode.ADD, fx.BOOKING_COUNT + 1),
        (OverlapMode.OVERWRITE, fx.BOOKING_COUNT + 1),
        (None, fx.BOOKING_COUNT),
    ],
)
def test_import_dialog_asks_on_overlap(
    qtbot, monkeypatch, app, statement_pdf, extended_pdf, choice, expected
) -> None:
    from PySide6.QtWidgets import QMessageBox

    from hiro_budget.gui.import_dialog import ImportDialog

    for name in ("information", "warning"):
        monkeypatch.setattr(QMessageBox, name, lambda *a, **k: None)
    with app.session() as s:
        import_service.import_file(s, app.settings, statement_pdf)

    asked = []
    monkeypatch.setattr(
        ImportDialog, "ask_overlap", lambda self, p: asked.append(p.path.name) or (choice, False)
    )
    dlg = ImportDialog(app, paths=[extended_pdf])
    qtbot.addWidget(dlg)
    assert "overlaps" in dlg.files.topLevelItem(0).text(7)
    dlg._import()
    assert asked == [extended_pdf.name]
    with app.session() as s:
        assert count_imported(s) == expected
        assert count(s, ImportBatch) == (1 if choice != OverlapMode.ADD else 2)


def test_overlap_message_box_builds(qtbot, app, statement_pdf, extended_pdf, monkeypatch) -> None:
    from PySide6.QtWidgets import QMessageBox

    from hiro_budget.gui.import_dialog import ImportDialog

    with app.session() as s:
        import_service.import_file(s, app.settings, statement_pdf)
        p = import_service.preview(s, extended_pdf)
    shown = {}

    def fake_exec(box):
        shown["text"] = box.text() + box.informativeText()
        shown["buttons"] = [b.text() for b in box.buttons()]
        next(b for b in box.buttons() if b.text() == "Overwrite").click()
        return 0

    monkeypatch.setattr(QMessageBox, "exec", fake_exec)
    dlg = ImportDialog(app)
    qtbot.addWidget(dlg)
    mode, apply_all = dlg.ask_overlap(p)
    assert mode == OverlapMode.OVERWRITE and apply_all is False
    assert len(shown["buttons"]) == 3
    assert str(fx.BOOKING_COUNT) in shown["text"]
