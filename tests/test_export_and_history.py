import csv
from pathlib import Path

import pytest
from openpyxl import load_workbook
from sqlalchemy import func, select

from hiro_budget.db.models import ImportBatch, Split, Transaction
from hiro_budget.services import export, import_service, reports
from hiro_budget.services import transactions as txs
from tests.fixtures import ing_statement as fx


@pytest.fixture
def imported(app, statement_pdf):
    with app.session() as s:
        import_service.import_file(s, app.settings, statement_pdf)
    return app


def count(session, model) -> int:
    return session.scalar(select(func.count()).select_from(model))


def count_imported(session) -> int:
    """Imported bookings (excludes automatic transfer counter-bookings)."""
    return session.scalar(
        select(func.count()).select_from(Transaction).where(Transaction.batch_id.is_not(None))
    )


# --- import history / delete ------------------------------------------------------


def test_list_and_delete_batch(imported, statement_pdf) -> None:
    with imported.session() as s:
        row = txs.list_transactions(s, txs.TxFilter(text="Handwerker"))[0]
        txs.set_notes(s, row.id, "Rechnung geprüft")
        txs.mark_transfer(s, row.id, 2)
        (batch,) = import_service.list_batches(s)
        assert batch.booking_count == fx.BOOKING_COUNT
        assert batch.manual_count == 1
        assert batch.account_name == "ING Girokonto"

    with imported.session() as s:
        assert import_service.delete_batch(s, imported.settings, batch.id) == fx.BOOKING_COUNT
    with imported.session() as s:
        assert count_imported(s) == 0
        assert count(s, Split) == 0
        assert count(s, ImportBatch) == 0
        # The file can be imported again.
        p = import_service.preview(s, statement_pdf)
        assert p.can_commit and p.new_count == fx.BOOKING_COUNT


def test_notes_roundtrip(imported) -> None:
    with imported.session() as s:
        row = txs.list_transactions(s)[0]
        txs.set_notes(s, row.id, "  Geschenk für Oma  ")
        assert txs.get_row(s, row.id).notes == "Geschenk für Oma"
        assert len(txs.list_transactions(s, txs.TxFilter(text="oma"))) == 1
        txs.set_notes(s, row.id, "   ")
        assert txs.get_row(s, row.id).notes is None


# --- export -----------------------------------------------------------------------


def test_export_transactions_csv_one_row_per_split(imported, tmp_path: Path) -> None:
    with imported.session() as s:
        rows = txs.list_transactions(s)
    path = tmp_path / "b.csv"
    export.export_transactions(path, rows, "de")
    raw = path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")  # BOM for Excel
    with path.open(encoding="utf-8-sig", newline="") as f:
        data = list(csv.reader(f, delimiter=";"))
    header, body = data[0], data[1:]
    assert header[0] == "Buchungsdatum" and "Kategorie" in header
    # +1: mortgage booking has two splits, +1: counter-booking on the loan account
    assert len(body) == fx.BOOKING_COUNT + 2
    mortgage = [r for r in body if r[header.index("Buchungsbetrag")] == "-1000,00"]
    assert sorted(r[header.index("Betrag")] for r in mortgage) == ["-400,00", "-600,00"]
    assert body[-1][0].count(".") == 2  # German date format


def test_export_transactions_xlsx(imported, tmp_path: Path) -> None:
    with imported.session() as s:
        rows = txs.list_transactions(s)
    path = tmp_path / "b.xlsx"
    export.export_transactions(path, rows, "en")
    ws = load_workbook(path)["Bookings"]
    assert ws["A1"].value == "Booking date"
    assert ws.max_row == fx.BOOKING_COUNT + 3  # header, mortgage split, loan counter-booking
    amount = ws.cell(row=2, column=8)
    assert isinstance(amount.value, int | float) and "€" in amount.number_format
    assert ws.cell(row=2, column=1).is_date


FORMULA_TEXTS = ["=1+1", '=HYPERLINK("https://x","Refund")', "+49 30", "-x", "@SUM(1)", "\tx"]


def test_export_writes_formula_like_texts_as_text(tmp_path: Path) -> None:
    """#74: booking texts come from third parties and must not become formulas."""
    keys = ["booking_date", "counterparty", "split_amount"]
    data = [[None, text, -12.5] for text in FORMULA_TEXTS]
    xlsx = tmp_path / "b.xlsx"
    export.write_xlsx(xlsx, [("sheet_bookings", keys, data)], "en")
    ws = load_workbook(xlsx)["Bookings"]
    cells = [ws.cell(row=r, column=2) for r in range(2, 2 + len(FORMULA_TEXTS))]
    assert [c.data_type for c in cells] == ["s"] * len(FORMULA_TEXTS)
    assert [c.value for c in cells] == FORMULA_TEXTS
    assert ws.cell(row=2, column=3).value == -12.5  # numbers stay numbers

    path = tmp_path / "b.csv"
    export.write_csv(path, keys, data, "de")
    with path.open(encoding="utf-8-sig", newline="") as f:
        body = list(csv.reader(f, delimiter=";"))[1:]
    assert [r[1] for r in body] == ["'" + t for t in FORMULA_TEXTS]
    assert {r[2] for r in body} == {"-12,50"}  # a negative amount gets no quote


def test_export_report(imported, tmp_path: Path) -> None:
    with imported.session() as s:
        months, cats = reports.monthly_totals(s), reports.category_totals(s)
    (xlsx,) = export.export_report(tmp_path / "r.xlsx", months, cats, "de")
    wb = load_workbook(xlsx)
    assert wb.sheetnames == ["Monate", "Kategorien"]
    assert wb["Monate"]["B2"].value == 4000.0
    paths = export.export_report(tmp_path / "r.csv", months, cats, "de")
    assert [p.name for p in paths] == ["r_monate.csv", "r_kategorien.csv"]
    assert "Unkategorisiert" in paths[1].read_text(encoding="utf-8-sig")
