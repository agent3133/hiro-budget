"""Manual bookings, transfer counter-bookings, matching on import, account balances."""

import sqlite3
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import select

from hiro_budget.config import OwnAccount, load_settings
from hiro_budget.db.models import Account, Category, SplitSource, Transaction
from hiro_budget.services import accounts as acc_service
from hiro_budget.services import config_edit, import_service
from hiro_budget.services import transactions as txs
from hiro_budget.services.import_service import OverlapMode
from hiro_budget.services.transactions import ManualEntry, SplitError
from tests.fixtures import ing_statement as fx


def acc_id(s, name: str) -> int:
    return s.scalar(select(Account.id).where(Account.name == name))


def cat_id(s, key: str) -> int:
    return s.scalar(select(Category.id).where(Category.key == key))


def rows_of(s, name: str) -> list[txs.TransactionRow]:
    return txs.list_transactions(s, txs.TxFilter(account_id=acc_id(s, name)))


def balance(s, name: str) -> int:
    return next(b for b in acc_service.account_balances(s) if b.name == name).balance_cents


# --- manual bookings ----------------------------------------------------------------


def test_manual_expense_with_category(app) -> None:
    with app.session() as s:
        tx_id = txs.create_manual(
            s,
            app.settings,
            ManualEntry(
                account_id=acc_id(s, "Bargeld"),
                booking_date=date(2026, 3, 5),
                amount_cents=-1250,
                payee="Wochenmarkt",
                category_id=cat_id(s, "lebensmittel.supermarkt"),
                notes="Gemüse",
            ),
        )
        row = txs.get_row(s, tx_id)
        assert row.is_manual_entry and row.is_manual and row.notes == "Gemüse"
        assert row.splits[0].category_key == "lebensmittel.supermarkt"
        assert balance(s, "Bargeld") == 100_00 - 12_50


def test_manual_entry_without_category_uses_rules(app) -> None:
    with app.session() as s:
        tx_id = txs.create_manual(
            s,
            app.settings,
            ManualEntry(acc_id(s, "Bargeld"), date(2026, 3, 5), -500, "Vodafone Shop"),
        )
        row = txs.get_row(s, tx_id)
        assert row.splits[0].source == SplitSource.RULE
        assert row.splits[0].category_key == "wohnen.internet_telefon"


def test_update_and_delete_manual(app) -> None:
    with app.session() as s:
        e = ManualEntry(acc_id(s, "Bargeld"), date(2026, 3, 5), -500, "Kiosk")
        tx_id = txs.create_manual(s, app.settings, e)
        e2 = txs.get_manual(s, tx_id)
        e2.amount_cents, e2.payee = 3000, "Flohmarkt Verkauf"
        e2.category_id = cat_id(s, "einkommen.sonstige_einnahmen")
        txs.update_manual(s, app.settings, tx_id, e2)
        row = txs.get_row(s, tx_id)
        assert (row.amount_cents, row.payee) == (3000, "Flohmarkt Verkauf")
        txs.delete_manual(s, tx_id)
        assert rows_of(s, "Bargeld") == []


def test_imported_bookings_cannot_be_deleted(app, statement_pdf) -> None:
    with app.session() as s:
        import_service.import_file(s, app.settings, statement_pdf)
        row = rows_of(s, "ING Girokonto")[0]
        with pytest.raises(SplitError):
            txs.delete_manual(s, row.id)


def test_zero_amount_rejected(app) -> None:
    with app.session() as s:
        with pytest.raises(SplitError):
            txs.create_manual(
                s, app.settings, ManualEntry(acc_id(s, "Bargeld"), date(2026, 3, 5), 0, "x")
            )


# --- transfer counter-bookings ------------------------------------------------------


def test_transfer_creates_linked_counter_booking(app, statement_pdf) -> None:
    with app.session() as s:
        import_service.import_file(s, app.settings, statement_pdf)
        hw = txs.list_transactions(s, txs.TxFilter(text="Handwerker"))[0]
        txs.mark_transfer(s, hw.id, acc_id(s, "Bargeld"))  # -120 from Giro -> +120 cash
        (cash,) = rows_of(s, "Bargeld")
        assert cash.amount_cents == 120_00 and cash.is_counterpart
        assert cash.counterpart_of == "ING Girokonto"
        assert balance(s, "Bargeld") == 100_00 + 120_00

        # Counter-bookings cannot be edited directly.
        with pytest.raises(SplitError):
            txs.set_category(s, cash.id, cat_id(s, "freizeit.restaurants"))

        # Changing the transfer back to a category removes the counter-booking.
        txs.set_category(s, hw.id, cat_id(s, "wohnen.instandhaltung"))
        assert rows_of(s, "Bargeld") == []


def test_manual_transfer_between_manual_accounts(app) -> None:
    with app.session() as s:
        tx_id = txs.create_manual(
            s,
            app.settings,
            ManualEntry(
                acc_id(s, "Bargeld"),
                date(2026, 3, 6),
                -5000,
                "Einzahlung",
                transfer_account_id=acc_id(s, "ING Extra-Konto"),
            ),
        )
        (savings,) = rows_of(s, "ING Extra-Konto")
        assert savings.amount_cents == 5000 and savings.is_counterpart
        txs.delete_manual(s, tx_id)  # deletes the counter-booking too
        assert rows_of(s, "ING Extra-Konto") == []


def test_no_counter_booking_when_mirror_exists(app) -> None:
    """Both sides already booked (e.g. both statements imported): nothing is created."""
    with app.session() as s:
        cash, savings = acc_id(s, "Bargeld"), acc_id(s, "ING Extra-Konto")
        a = txs.create_manual(
            s, app.settings, ManualEntry(cash, date(2026, 3, 6), -5000, "an Extra")
        )
        b = txs.create_manual(
            s, app.settings, ManualEntry(savings, date(2026, 3, 7), 5000, "von Bargeld")
        )
        txs.mark_transfer(s, a, savings)  # b is not a transfer yet: counterpart on savings
        txs.mark_transfer(s, b, cash)  # a transfers to savings: a is b's mirror
        txs.mark_transfer(s, a, savings)  # re-sync a: b is now its mirror, counterpart goes
        assert [r.id for r in rows_of(s, "Bargeld")] == [a]
        assert [r.id for r in rows_of(s, "ING Extra-Konto")] == [b]


def test_mortgage_principal_creates_loan_counter_booking(app, statement_pdf) -> None:
    with app.session() as s:
        import_service.import_file(s, app.settings, statement_pdf)
        (loan,) = rows_of(s, "ING Baufinanzierung")
        assert loan.amount_cents == 400_00 and loan.is_counterpart


# --- matching on import -------------------------------------------------------------


def test_import_replaces_matching_manual_entry(app, statement_pdf) -> None:
    with app.session() as s:
        manual = txs.create_manual(
            s,
            app.settings,
            ManualEntry(
                acc_id(s, "ING Girokonto"),
                date(2026, 3, 8),  # bank booked it on 10.03.
                -120_00,
                "Handwerker",
                category_id=cat_id(s, "wohnen.instandhaltung"),
                notes="Heizung",
            ),
        )
        p = import_service.preview(s, statement_pdf)
        assert list(p.matches) == [5]  # index of the Handwerker booking
    with app.session() as s:
        r = import_service.import_file(s, app.settings, statement_pdf)
        assert r.matched == 1
        assert s.get(Transaction, manual) is None
        hw = txs.list_transactions(s, txs.TxFilter(text="Handwerker GmbH"))[0]
        assert hw.is_manual and hw.notes == "Heizung" and not hw.is_manual_entry
        assert hw.splits[0].category_key == "wohnen.instandhaltung"


def test_no_match_outside_window(app, statement_pdf) -> None:
    with app.session() as s:
        txs.create_manual(
            s,
            app.settings,
            ManualEntry(acc_id(s, "ING Girokonto"), date(2026, 2, 20), -120_00, "Handwerker"),
        )
        r = import_service.import_file(s, app.settings, statement_pdf)
        assert r.matched == 0
        assert len(rows_of(s, "ING Girokonto")) == fx.BOOKING_COUNT + 1


def test_import_replaces_counter_booking_from_other_account(app, statement_pdf) -> None:
    """Cash refund entered as transfer from Giro; the Giro statement later contains it."""
    with app.session() as s:
        txs.create_manual(
            s,
            app.settings,
            ManualEntry(
                acc_id(s, "Bargeld"),
                date(2026, 3, 9),
                120_00,
                "Abhebung",
                transfer_account_id=acc_id(s, "ING Girokonto"),
            ),
        )
        assert len(rows_of(s, "ING Girokonto")) == 1  # counter-booking -120
    with app.session() as s:
        r = import_service.import_file(s, app.settings, statement_pdf)
        assert r.matched == 1
        giro_rows = rows_of(s, "ING Girokonto")
        assert len(giro_rows) == fx.BOOKING_COUNT
        hw = next(r for r in giro_rows if r.payee == "Handwerker GmbH")
        assert hw.splits[0].transfer_account_name == "Bargeld"
        # The cash side is not duplicated: the imported booking is its mirror.
        assert len(rows_of(s, "Bargeld")) == 1


def test_overwrite_keeps_manual_entries(app, statement_pdf, tmp_path: Path) -> None:
    with app.session() as s:
        import_service.import_file(s, app.settings, statement_pdf)
        txs.create_manual(
            s,
            app.settings,
            ManualEntry(acc_id(s, "ING Girokonto"), date(2026, 3, 20), -777, "Bäcker bar"),
        )
    copy = tmp_path / "copy.pdf"
    copy.write_bytes(statement_pdf.read_bytes() + b"\n%")
    with app.session() as s:
        p = import_service.preview(s, copy)
        assert p.overlap.existing_count == fx.BOOKING_COUNT  # manual entry not counted
        import_service.commit(s, app.settings, p, mode=OverlapMode.OVERWRITE)
    with app.session() as s:
        assert any(r.payee == "Bäcker bar" for r in rows_of(s, "ING Girokonto"))


# --- balances and account editing ---------------------------------------------------


def test_balances(app, statement_pdf) -> None:
    with app.session() as s:
        import_service.import_file(s, app.settings, statement_pdf)
        balances = {b.name: b for b in acc_service.account_balances(s)}
        giro = balances["ING Girokonto"]
        assert giro.anchor == "statement" and giro.balance_cents == fx.CLOSING_CENTS
        assert giro.statement_balance_cents == fx.CLOSING_CENTS
        assert balances["Bargeld"].anchor == "opening"
        assert balances["Bargeld"].balance_cents == 100_00
        assert balances["ING Baufinanzierung"].balance_cents == 400_00  # principal paid


def test_save_accounts_rename_and_add(app, config_dir) -> None:
    accounts = [a.model_copy() for a in app.settings.app.own_accounts]
    loan = next(a for a in accounts if a.kind == "loan")
    loan.name = "Baukredit"
    accounts.append(
        OwnAccount(
            name="Kasse Kinder", kind="cash", opening_balance=20.5, opening_date=date(2026, 1, 1)
        )
    )
    with app.session() as s:
        acc_service.rename_account(s, "ING Baufinanzierung", "Baukredit")
        config_edit.save_accounts(
            app.settings, accounts, renames={"ING Baufinanzierung": "Baukredit"}
        )
    app.reload_config()
    reloaded = load_settings(config_dir)
    assert reloaded.app.mortgage[0].loan_account == "Baukredit"
    assert any(a.name == "Kasse Kinder" and a.kind == "cash" for a in reloaded.app.own_accounts)
    with app.session() as s:
        kasse = s.scalar(select(Account).where(Account.name == "Kasse Kinder"))
        assert kasse.opening_balance_cents == 2050
        assert s.scalar(select(Account).where(Account.name == "ING Baufinanzierung")) is None


def test_save_accounts_rejects_removing_loan_account(app) -> None:
    accounts = [a for a in app.settings.app.own_accounts if a.kind != "loan"]
    with pytest.raises(Exception, match="loan_account"):
        config_edit.save_accounts(app.settings, accounts)


def test_delete_account_only_without_bookings(app, statement_pdf) -> None:
    with app.session() as s:
        import_service.import_file(s, app.settings, statement_pdf)
        with pytest.raises(acc_service.AccountError):
            acc_service.delete_account(s, "ING Girokonto")
        acc_service.delete_account(s, "ING Extra-Konto")
        assert s.scalar(select(Account).where(Account.name == "ING Extra-Konto")) is None


# --- migration safety ---------------------------------------------------------------


def test_migration_keeps_splits(tmp_path: Path) -> None:
    """Rebuilding tables in batch migrations must not cascade-delete splits."""
    from alembic import command

    from hiro_budget.db.session import alembic_config, make_engine

    db = tmp_path / "old.db"
    engine = make_engine(db)
    command.upgrade(alembic_config(engine), "0001")
    con = sqlite3.connect(db)
    con.executescript(
        """
        insert into account (id, name, kind, currency, is_own) values (1, 'G', 'giro', 'EUR', 1);
        insert into "transaction" (id, account_id, booking_date, amount_cents, currency,
            booking_type, counterparty, purpose, raw_text, dedupe_key)
            values (1, 1, '2026-01-01', -100, 'EUR', 'x', '', '', '', 'k1');
        insert into split (id, transaction_id, amount_cents, source)
            values (1, 1, -100, 'rule');
        """
    )
    con.commit()
    con.close()
    command.upgrade(alembic_config(engine), "head")
    con = sqlite3.connect(db)
    assert con.execute("select count(*) from split").fetchone()[0] == 1
    con.close()


def test_startup_creates_missing_counter_bookings(app, config_dir, statement_pdf) -> None:
    """Transfers stored before counter-bookings existed get them on the next start."""
    from hiro_budget.app import App

    with app.session() as s:
        import_service.import_file(s, app.settings, statement_pdf)
        for c in s.scalars(select(Transaction).where(Transaction.counterpart_of_id.is_not(None))):
            s.delete(c)  # simulate an old database
    with app.session() as s:
        assert rows_of(s, "ING Baufinanzierung") == []
    restarted = App.from_config_dir(config_dir)
    with restarted.session() as s:
        (loan,) = rows_of(s, "ING Baufinanzierung")
        assert loan.amount_cents == 400_00
    App.from_config_dir(config_dir)  # idempotent
    with restarted.session() as s:
        assert len(rows_of(s, "ING Baufinanzierung")) == 1


# --- budget relevance of transfers to tracking accounts ------------------------------


def test_principal_category_on_mortgage(app, config_dir, statement_pdf) -> None:
    from ruamel.yaml import YAML

    from hiro_budget.app import App

    y = YAML()
    cfg_file = config_dir / "config.yaml"
    cfg = y.load(cfg_file.read_text(encoding="utf-8"))
    cfg["mortgage"][0]["principal_category"] = "wohnen.baufinanzierung"
    with cfg_file.open("w", encoding="utf-8") as f:
        y.dump(cfg, f)

    app2 = App.from_config_dir(config_dir)
    with app2.session() as s:
        import_service.import_file(s, app2.settings, statement_pdf)
        mortgage = txs.list_transactions(s, txs.TxFilter(text="ZINSEN"))[0]
        principal = mortgage.splits[1]
        assert principal.category_key == "wohnen.baufinanzierung"
        assert principal.transfer_account_name == "ING Baufinanzierung"
        assert not mortgage.is_uncategorized
        assert all(
            r.id != mortgage.id
            for r in txs.list_transactions(s, txs.TxFilter(category=txs.UNCATEGORIZED))
        )


def test_transfer_to_tracking_account_needs_category(app, statement_pdf) -> None:
    with app.session() as s:
        import_service.import_file(s, app.settings, statement_pdf)
        hw = txs.list_transactions(s, txs.TxFilter(text="Handwerker"))[0]
        loan = acc_id(s, "ING Baufinanzierung")
        txs.mark_transfer(s, hw.id, loan)
        row = txs.get_row(s, hw.id)
        assert row.is_uncategorized  # leaves the budget without a category
        # Assigning a category keeps the transfer.
        txs.set_category(s, hw.id, cat_id(s, "wohnen.instandhaltung"))
        row = txs.get_row(s, hw.id)
        assert row.splits[0].transfer_account_name == "ING Baufinanzierung"
        assert row.splits[0].category_key == "wohnen.instandhaltung"
        assert not row.is_uncategorized
        # Between two budget accounts a category is not allowed.
        with pytest.raises(SplitError):
            txs.set_splits(
                s,
                hw.id,
                [
                    txs.SplitInput(
                        -120_00, cat_id(s, "wohnen.energie"), acc_id(s, "ING Extra-Konto")
                    )
                ],
            )


def test_on_budget_defaults(app) -> None:
    with app.session() as s:
        flags = {a.name: a.on_budget for a in s.scalars(select(Account))}
    assert flags == {
        "ING Girokonto": True,
        "ING Extra-Konto": True,
        "ING Baufinanzierung": False,
        "Bargeld": True,
    }
