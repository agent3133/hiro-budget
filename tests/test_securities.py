"""Extra-Konto (yearly statement), Depotauszug valuations and transfers to the depot."""

from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import func, select

from hiro_budget.config import ConfigError, load_settings
from hiro_budget.db.models import Account, AccountKind, Holding, Transaction
from hiro_budget.importers.base import find_importer
from hiro_budget.plugins.ing_depot_pdf import importer as ing_depot_pdf
from hiro_budget.plugins.ing_giro_pdf import importer as ing_giro_pdf
from hiro_budget.services import accounts as acc_service
from hiro_budget.services import budget, import_service
from hiro_budget.services.counterparts import new_manual_key
from tests.conftest import TEST_CONFIG, write_statement_pdf
from tests.fixtures import ing_securities as fx

SECURITIES_CONFIG = TEST_CONFIG.replace(
    "    kind: savings\n", f"    kind: savings\n    iban: {fx.EXTRA_KONTO_IBAN}\n"
).replace(
    "  - name: ING Baufinanzierung\n",
    f"""  - name: ING Direkt-Depot
    kind: depot
    iban: "{fx.DEPOT_NO}"
    match: {{purpose: "(?i)WP-ABRECHNUNG"}}
    transfer_category: sparen.wertpapiere
  - name: ING Baufinanzierung
""",
)


@pytest.fixture
def app(config_dir: Path):
    from hiro_budget.app import App

    (config_dir / "config.yaml").write_text(SECURITIES_CONFIG, encoding="utf-8")
    return App.from_config_dir(config_dir)


@pytest.fixture
def extra_pdf(tmp_path: Path) -> Path:
    return write_statement_pdf(tmp_path / "Extra_Konto.pdf", [fx.EXTRA_KONTO_PAGE])


@pytest.fixture
def depot_pdf(tmp_path: Path) -> Path:
    return write_statement_pdf(tmp_path / "Direkt_Depot.pdf", [fx.DEPOT_PAGE])


def account(s, name: str) -> Account:
    return s.scalar(select(Account).where(Account.name == name))


def balance(s, name: str) -> int:
    return next(b.balance_cents for b in acc_service.account_balances(s) if b.name == name)


# --- parsing -----------------------------------------------------------------------


def test_extra_konto_yearly_statement() -> None:
    stmt = ing_giro_pdf.parse_pages([fx.EXTRA_KONTO_PAGE])
    assert stmt.warnings == []
    assert stmt.balance_ok
    assert stmt.account_kind == "savings"
    assert (stmt.period_start, stmt.period_end) == (date(2025, 1, 1), date(2025, 12, 31))
    types = [t.booking_type for t in stmt.transactions]
    assert types == [
        "Wertpapiergutschrift",
        "Zins/Dividende WP",
        "Wertpapierkauf",
        "Lastschrift-Einzug",
        "Kapitalertragsteuer",
        "Solidaritätszuschlag",
        "Zinsertrag",
    ]
    assert stmt.transactions[1].counterparty == ""
    assert stmt.transactions[3].counterparty == "Erika Musterfrau"


def test_depot_statement() -> None:
    stmt = ing_depot_pdf.parse_pages([fx.DEPOT_PAGE])
    assert stmt.warnings == []
    assert stmt.valuation and stmt.balance_ok
    assert stmt.account_name == "ING Direkt-Depot 1122334455"
    assert stmt.account_iban == fx.DEPOT_NO
    assert stmt.account_kind == "depot"
    assert stmt.period_end == date(2026, 9, 30)
    assert stmt.statement_date == date(2026, 10, 12)
    assert stmt.closing_balance_cents == fx.DEPOT_TOTAL_CENTS
    world, europe = stmt.holdings
    assert (world.name, world.isin, world.wkn) == (
        "MUSTER WORLD UCITS ETF",
        "IE0000000001",
        "A0AAA1",
    )
    assert (world.quantity, world.unit, world.price, world.price_currency) == (
        100,
        "Stück",
        50.0,
        "EUR",
    )
    assert world.value_cents == 5_000_00
    assert europe.quantity == pytest.approx(1234.56789)
    assert europe.price == pytest.approx(10.123)
    assert europe.isin == "LU0000000002"


def test_depot_statement_with_missing_position_fails_the_check() -> None:
    page = fx.DEPOT_PAGE.replace("100 Stück MUSTER WORLD", "100 Stk MUSTER WORLD")
    stmt = ing_depot_pdf.parse_pages([page])
    assert not stmt.balance_ok
    assert stmt.balance_difference_cents == 5_000_00
    assert any("2 positions announced, 1 recognized" in w for w in stmt.warnings)


def test_importers_are_told_apart(extra_pdf, depot_pdf) -> None:
    assert find_importer(extra_pdf).name == ing_giro_pdf.IngGiroPdfImporter.name
    assert find_importer(depot_pdf).name == ing_depot_pdf.IngDepotPdfImporter.name


# --- valuations ----------------------------------------------------------------------


def test_import_valuation(app, depot_pdf) -> None:
    with app.session() as s:
        p = import_service.preview(s, depot_pdf)
        assert p.can_commit and p.errors == [] and p.account_name == "ING Direkt-Depot"
        r = import_service.commit(s, app.settings, p)
        assert (r.added, r.holdings) == (0, 2)
    with app.session() as s:
        depot = account(s, "ING Direkt-Depot")
        assert depot.kind == AccountKind.DEPOT and not depot.on_budget
        assert balance(s, "ING Direkt-Depot") == fx.DEPOT_TOTAL_CENTS
        [v] = acc_service.valuations(s, depot.id)
        assert v.date == date(2026, 9, 30)
        assert [h.isin for h in v.holdings] == ["IE0000000001", "LU0000000002"]
        [info] = import_service.list_batches(s)
        assert (info.valuation, info.holding_count, info.booking_count) == (True, 2, 0)


def test_valuation_creates_unknown_depot_account(config_dir, depot_pdf) -> None:
    from hiro_budget.app import App

    app = App.from_config_dir(config_dir)  # test config without a depot
    with app.session() as s:
        import_service.import_file(s, app.settings, depot_pdf)
    with app.session() as s:
        depot = account(s, "ING Direkt-Depot 1122334455")
        assert depot.iban == fx.DEPOT_NO and not depot.on_budget


def test_balance_is_latest_valuation_plus_later_bookings(app, depot_pdf) -> None:
    with app.session() as s:
        import_service.import_file(s, app.settings, depot_pdf)
        depot = account(s, "ING Direkt-Depot")
        for day, cents in ((date(2026, 9, 30), 111_00), (date(2026, 10, 5), 1_000_00)):
            s.add(
                Transaction(
                    account=depot,
                    booking_date=day,
                    amount_cents=cents,
                    booking_type="Umbuchung",
                    dedupe_key=new_manual_key(),
                )
            )
    with app.session() as s:
        # Bookings up to the valuation date are contained in the value.
        assert balance(s, "ING Direkt-Depot") == fx.DEPOT_TOTAL_CENTS + 1_000_00
        b = next(b for b in acc_service.account_balances(s) if b.name == "ING Direkt-Depot")
        assert (b.anchor, b.statement_date) == ("valuation", date(2026, 9, 30))


def add_depot_trades(s) -> None:
    """Bookings around the valuation of 30.09.2026 (see fx.DEPOT_PAGE)."""
    depot, extra = account(s, "ING Direkt-Depot"), account(s, "ING Extra-Konto")

    def tx(acc, day, cents, purpose="", counterpart_of=None) -> Transaction:
        t = Transaction(
            account=acc,
            booking_date=day,
            amount_cents=cents,
            booking_type="Umbuchung",
            purpose=purpose,
            counterpart_of=counterpart_of,
            dedupe_key=new_manual_key(),
        )
        s.add(t)
        return t

    # contained in the valuation
    tx(depot, date(2026, 9, 30), 99_00, "WP-ABRECHNUNG 1 Kauf ISIN IE0000000001 MUSTER")
    # ISIN in the booking itself
    tx(depot, date(2026, 10, 5), 1_000_00, "WP-ABRECHNUNG 2 Kauf ISIN IE0000000001 MUSTER")
    # counter-booking of a purchase on the Extra-Konto: ISIN in the original's text
    original = tx(
        extra, date(2026, 10, 6), -7_000_00, "WP-ABRECHNUNG3KaufISINLU9999999999MONEY MARKET"
    )
    tx(depot, date(2026, 10, 6), 7_000_00, counterpart_of=original)
    tx(depot, date(2026, 10, 7), 10_00)  # no ISIN
    s.flush()


def test_positions_per_isin(app, depot_pdf) -> None:
    with app.session() as s:
        import_service.import_file(s, app.settings, depot_pdf)
        add_depot_trades(s)
    with app.session() as s:
        p = acc_service.positions(s, account(s, "ING Direkt-Depot").id)
        assert p.statement_date == date(2026, 9, 30)
        rows = [(x.isin, x.name, x.statement_cents, x.value_cents) for x in p.positions]
        assert rows == [
            ("IE0000000001", "MUSTER WORLD UCITS ETF", 5_000_00, 6_000_00),
            ("LU0000000002", "MUSTER EUROPE ETF", 12_497_53, 12_497_53),
            ("LU9999999999", "MONEY MARKET", 0, 7_000_00),
            (None, "", 0, 10_00),
        ]
        assert p.positions[2].trades[0].date == date(2026, 10, 6)
        assert p.total_cents == balance(s, "ING Direkt-Depot")
        assert acc_service.positions(s, account(s, "ING Girokonto").id) is None


def test_accounts_tab_lists_positions(qtbot, app, depot_pdf) -> None:
    from hiro_budget.gui.accounts_view import AccountsView

    with app.session() as s:
        import_service.import_file(s, app.settings, depot_pdf)
        add_depot_trades(s)
    view = AccountsView(app)
    qtbot.addWidget(view)
    depot = view.table.topLevelItem([d.name for d in view.drafts].index("ING Direkt-Depot"))
    assert depot.isExpanded() and depot.childCount() == 4
    new = depot.child(2)
    assert new.text(1) == "LU9999999999"
    assert new.text(2).endswith(" *")  # at cost
    assert "06.10.2026" in new.toolTip(2) or "10/6/26" in new.toolTip(2)
    assert depot.child(1).text(2) in ("12.497,53 €", "€12,497.53", "12,497.53 €")
    view.table.setCurrentItem(new)  # selects the depot's settings
    assert view.name.text() == "ING Direkt-Depot"
    depot.setExpanded(False)
    view.reload_balances()
    depot = view.table.topLevelItem([d.name for d in view.drafts].index("ING Direkt-Depot"))
    assert not depot.isExpanded()  # remembered


def test_newer_valuation_wins(app, depot_pdf, tmp_path) -> None:
    october = (
        fx.DEPOT_PAGE.replace("per 30.09.2026", "per 31.10.2026")
        .replace("50,00 EUR 5.000,00 EUR", "51,00 EUR 5.100,00 EUR")
        .replace("Gesamtkurswert 17.497,53", "Gesamtkurswert 17.597,53")
    )
    october_pdf = write_statement_pdf(tmp_path / "october.pdf", [october])
    with app.session() as s:
        import_service.import_file(s, app.settings, october_pdf)
        import_service.import_file(s, app.settings, depot_pdf)  # older one imported later
    with app.session() as s:
        assert balance(s, "ING Direkt-Depot") == 17_597_53
        dates = [v.date for v in acc_service.valuations(s, account(s, "ING Direkt-Depot").id)]
        assert dates == [date(2026, 10, 31), date(2026, 9, 30)]


def test_valuation_of_same_date_replaces_the_stored_one(app, depot_pdf, tmp_path) -> None:
    again = write_statement_pdf(tmp_path / "again.pdf", [fx.DEPOT_PAGE + "Kopie\n"])
    with app.session() as s:
        import_service.import_file(s, app.settings, depot_pdf)
    with app.session() as s:
        p = import_service.preview(s, again)
        assert p.can_commit and p.replaces_valuation == depot_pdf.name
        r = import_service.commit(s, app.settings, p)
        assert r.replaced == 1
    with app.session() as s:
        assert [b.source_file for b in import_service.list_batches(s)] == ["again.pdf"]
        assert s.scalar(select(func.count()).select_from(Holding)) == 2


def test_same_file_is_not_imported_twice(app, depot_pdf) -> None:
    with app.session() as s:
        import_service.import_file(s, app.settings, depot_pdf)
    with app.session() as s:
        p = import_service.preview(s, depot_pdf)
        assert not p.can_commit and p.file_already_imported


def test_delete_valuation(app, depot_pdf) -> None:
    with app.session() as s:
        import_service.import_file(s, app.settings, depot_pdf)
    with app.session() as s:
        [info] = import_service.list_batches(s)
        import_service.delete_batch(s, app.settings, info.id)
    with app.session() as s:
        assert s.scalar(select(func.count()).select_from(Holding)) == 0
        assert balance(s, "ING Direkt-Depot") == 0


# --- Extra-Konto bookings ------------------------------------------------------------


def _splits(s) -> dict[str, tuple[str | None, str | None]]:
    """booking type -> (category key, transfer account) of the Extra-Konto bookings."""
    extra = account(s, "ING Extra-Konto")
    out = {}
    for tx in s.scalars(select(Transaction).where(Transaction.account_id == extra.id)):
        [sp] = tx.splits
        out[f"{tx.booking_type} {tx.amount_cents}"] = (
            sp.category.key if sp.category else None,
            sp.transfer_account.name if sp.transfer_account else None,
        )
    return out


def test_extra_konto_categorization(app, extra_pdf) -> None:
    with app.session() as s:
        r = import_service.import_file(s, app.settings, extra_pdf)
        assert r.added == fx.EXTRA_KONTO_BOOKINGS
    with app.session() as s:
        assert _splits(s) == {
            # sale: back into the budget, uncategorized (Ready to Assign)
            "Wertpapiergutschrift 100000": (None, "ING Direkt-Depot"),
            "Zins/Dividende WP 5000": ("einkommen.kapitalertraege", None),
            # purchase: leaves the budget into the depot (tracking) -> budget category
            "Wertpapierkauf -30000": ("sparen.wertpapiere", "ING Direkt-Depot"),
            "Lastschrift-Einzug 50000": (None, None),
            "Kapitalertragsteuer -500": ("steuern.kapitalertragsteuer", None),
            "Solidaritätszuschlag -27": ("steuern.kapitalertragsteuer", None),
            "Zinsertrag 2000": ("einkommen.kapitalertraege", None),
        }
        assert balance(s, "ING Extra-Konto") == 1_364_73
        # Counter-bookings on the depot (no valuation yet: sum of transfers).
        assert balance(s, "ING Direkt-Depot") == -1_000_00 + 300_00


def test_depot_purchase_is_budget_activity(app, extra_pdf) -> None:
    with app.session() as s:
        import_service.import_file(s, app.settings, extra_pdf)
        b = budget.compute_month(s, app.settings, "2025-02")
        wp = next(c for c in b.categories if c.key == "sparen.wertpapiere")
        assert wp.activity == -300_00
        assert b.ready_to_assign + b.available + b.assigned_in_future == budget.budget_balance(
            s, "2025-02"
        )


def test_transfer_category_must_exist(config_dir) -> None:
    (config_dir / "config.yaml").write_text(
        SECURITIES_CONFIG.replace("sparen.wertpapiere", "sparen.gibtsnicht"), encoding="utf-8"
    )
    with pytest.raises(ConfigError, match="sparen.gibtsnicht"):
        load_settings(config_dir)


# --- GUI -----------------------------------------------------------------------------


def test_import_dialog_shows_holdings(qtbot, app, depot_pdf) -> None:
    from hiro_budget.gui.import_dialog import ImportDialog

    dlg = ImportDialog(app, paths=[depot_pdf])
    qtbot.addWidget(dlg)
    item = dlg.files.topLevelItem(0)
    assert item.text(1) == "ING Direkt-Depot"
    assert item.text(3) == ""  # no bookings column for a valuation
    assert "2" in item.text(7)
    assert dlg.preview.rowCount() == 2
    assert dlg.preview.item(0, 1).text() == "IE0000000001"
    assert dlg.preview.horizontalHeaderItem(0).text() == "Security"
    assert dlg.import_button.isEnabled()


def test_holdings_dialog_and_accounts_tab(qtbot, app, depot_pdf) -> None:
    from hiro_budget.gui.accounts_view import AccountsView
    from hiro_budget.gui.holdings_dialog import HoldingsDialog

    with app.session() as s:
        import_service.import_file(s, app.settings, depot_pdf)
        depot_id = account(s, "ING Direkt-Depot").id
    view = AccountsView(app)
    qtbot.addWidget(view)
    view.select_account("ING Direkt-Depot")
    assert view.holdings_button.isEnabled()
    assert view.transfer_category.isEnabled()  # tracking account
    assert view.transfer_category.currentData() == "sparen.wertpapiere"
    view.select_account("ING Girokonto")
    assert not view.holdings_button.isEnabled()
    assert not view.transfer_category.isEnabled()  # budget account

    dlg = HoldingsDialog(app, depot_id, "ING Direkt-Depot")
    qtbot.addWidget(dlg)
    assert dlg.table.rowCount() == 2
    # number format follows the UI language (German or English)
    assert dlg.table.item(1, 3).text()[:11] in ("1.234,56789", "1,234.56789")
    assert dlg.table.item(0, 6).text() in ("28,6 %", "28.6 %")


def test_accounts_tab_saves_transfer_category(qtbot, app, config_dir) -> None:
    from hiro_budget.gui.accounts_view import AccountsView

    view = AccountsView(app)
    qtbot.addWidget(view)
    view.select_account("ING Direkt-Depot")
    view.transfer_category.setCurrentIndex(view.transfer_category.findData(""))
    view.save()
    depot = next(a for a in load_settings(config_dir).app.own_accounts if a.kind == "depot")
    assert depot.transfer_category is None


def test_yearly_depot_statement() -> None:
    page = fx.DEPOT_PAGE.replace(
        "Depotauszug per 30.09.2026",
        "Jahresdepotauszug\nwir übersenden Ihnen die zum 31.12.2025 abgeschlossene Aufstellung",
    )
    stmt = ing_depot_pdf.parse_pages([page])
    assert stmt.warnings == []
    assert stmt.period_end == date(2025, 12, 31)
