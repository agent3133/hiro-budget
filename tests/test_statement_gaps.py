"""Gaps in the chain of an account's statements (missing statements)."""

from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import select

from hiro_budget.db.models import Account, ImportBatch
from hiro_budget.gui.accounts_view import AccountsView
from hiro_budget.gui.import_dialog import ImportDialog
from hiro_budget.importers.base import ParsedStatement, ParsedTransaction
from hiro_budget.services import accounts as acc_service
from hiro_budget.services import import_service
from hiro_budget.services.accounts import StatementLink, statement_gaps

IBAN = "DE12345678901234567890"  # ING Girokonto in the test configuration


def link(name, start, end, opening, closing) -> StatementLink:
    return StatementLink(name, start, end, opening, closing)


def test_connected_statements_have_no_gap() -> None:
    links = [
        link("feb", date(2026, 2, 1), date(2026, 2, 28), 200, 300),
        link("jan", date(2026, 1, 1), date(2026, 1, 31), 100, 200),
    ]
    assert statement_gaps(links) == []


def test_missing_month_is_a_gap() -> None:
    jan = link("jan", date(2026, 1, 1), date(2026, 1, 31), 100, 200)
    mar = link("mar", date(2026, 3, 1), date(2026, 3, 31), 250, 400)
    [gap] = statement_gaps([mar, jan])
    assert (gap.after, gap.before, gap.difference_cents) == (jan, mar, 50)


def test_overlapping_statements_are_not_compared() -> None:
    # Umsatzanzeige of part of October, then the Kontoauszug of the whole month
    links = [
        link("ums", date(2026, 10, 1), date(2026, 10, 21), 100, 150),
        link("oct", date(2026, 10, 1), date(2026, 10, 31), 100, 180),
        link("nov", date(2026, 11, 1), date(2026, 11, 30), 180, 90),
    ]
    assert statement_gaps(links) == []


def statement(name: str, start: date, end: date, opening: int, closing: int) -> ParsedStatement:
    return ParsedStatement(
        importer="fake",
        institution="ING",
        account_name="ING Girokonto 1234567890",
        account_kind="giro",
        account_iban=IBAN,
        period_start=start,
        period_end=end,
        opening_balance_cents=opening,
        closing_balance_cents=closing,
        transactions=[
            ParsedTransaction(
                booking_date=end,
                amount_cents=closing - opening,
                booking_type="Gutschrift",
                counterparty=name,
            )
        ],
    )


STATEMENTS = {
    "sep.pdf": statement("sep", date(2026, 9, 1), date(2026, 9, 30), 1_500_00, 1_800_00),
    "oct.pdf": statement("oct", date(2026, 10, 1), date(2026, 10, 31), 1_800_00, 2_000_00),
}


class FakeImporter:
    name = "fake"

    def detect(self, path: Path) -> bool:
        return True

    def parse(self, path: Path) -> ParsedStatement:
        return STATEMENTS[path.name]


@pytest.fixture
def files(app, tmp_path: Path, monkeypatch) -> dict[str, Path]:
    monkeypatch.setattr(import_service, "find_importer", lambda path, extra=None: FakeImporter())
    paths = {}
    for name in STATEMENTS:
        paths[name] = tmp_path / name
        paths[name].write_bytes(name.encode())
    with app.session() as s:
        giro = s.scalar(select(Account).where(Account.name == "ING Girokonto"))
        s.add(
            ImportBatch(
                account=giro,
                source_file="aug.pdf",
                file_sha256="aug",
                importer="fake",
                period_start=date(2026, 8, 1),
                period_end=date(2026, 8, 31),
                opening_balance_cents=1_000_00,
                closing_balance_cents=1_200_00,
            )
        )
    return paths


def test_preview_reports_gap_to_stored_statements(app, files) -> None:
    with app.session() as s:
        p = import_service.preview(s, files["oct.pdf"])
        [gap] = p.gaps
        assert (gap.after.source_file, gap.before.source_file) == ("aug.pdf", "oct.pdf")
        assert gap.difference_cents == 600_00
        assert p.can_commit  # a warning, not an error


def test_account_balance_lists_gaps(app, files) -> None:
    with app.session() as s:
        import_service.commit(s, app.settings, import_service.preview(s, files["oct.pdf"]))
    with app.session() as s:
        b = next(b for b in acc_service.account_balances(s) if b.name == "ING Girokonto")
        assert [g.difference_cents for g in b.gaps] == [600_00]


def test_accounts_tab_marks_balance(qtbot, app, files) -> None:
    with app.session() as s:
        import_service.commit(s, app.settings, import_service.preview(s, files["oct.pdf"]))
    view = AccountsView(app)
    qtbot.addWidget(view)
    row = next(i for i, d in enumerate(view.drafts) if d.name == "ING Girokonto")
    item = view.table.topLevelItem(row)
    assert item.text(2).startswith("⚠")
    assert "01.09.2026" in item.toolTip(2) or "9/1/26" in item.toolTip(2)


def test_import_dialog_gap_filled_by_another_file(qtbot, app, files) -> None:
    dlg = ImportDialog(app, paths=[files["oct.pdf"]])
    qtbot.addWidget(dlg)
    oct_entry = dlg.entries[0]
    assert len(dlg._open_gaps(oct_entry)) == 1
    assert dlg.files.topLevelItem(0).text(7).endswith(dlg.tr("statements missing, see below"))
    # The September statement closes the gap except for its own start.
    dlg.add_paths([files["sep.pdf"]])
    assert dlg._open_gaps(oct_entry) == []
    assert [g.difference_cents for g in dlg._open_gaps(dlg.entries[1])] == [300_00]
