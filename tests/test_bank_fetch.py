"""Fetching from the bank (FinTS, issues #7 and #8) against a fake bank."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from hiro_budget.config import (
    BankAccountLink,
    BankLogin,
    FintsConfig,
    OwnAccount,
    load_settings,
)
from hiro_budget.db.models import Account, ImportBatch
from hiro_budget.importers.base import ParsedTransaction
from hiro_budget.services import bank, config_edit, import_service
from hiro_budget.services.accounts import account_balances
from hiro_budget.services.bank import BankAccount, Fetched, fetch

IBAN = "DE12345678901234567890"  # the test statement's account
TODAY = date(2026, 4, 20)


def tx(day: date, cents: int, counterparty: str = "REWE", purpose: str = "Einkauf"):
    return ParsedTransaction(
        booking_date=day,
        value_date=day,
        amount_cents=cents,
        booking_type="Lastschrift",
        counterparty=counterparty,
        purpose=purpose,
    )


class FakeBank:
    """Accounts with bookings and balances; ``connect`` checks the PIN."""

    def __init__(self) -> None:
        self.offered = [
            BankAccount(IBAN, "Girokonto", "giro", IBAN),
            BankAccount(
                "DE99999999999999999999", "Extra-Konto", "savings", "DE99999999999999999999"
            ),
            BankAccount("1234567890", "Direkt-Depot", "depot"),
        ]
        self.bookings: dict[str, list[ParsedTransaction]] = {a.bank_id: [] for a in self.offered}
        self.balances: dict[str, int] = {a.bank_id: 0 for a in self.offered}
        self.pin = "1234"
        self.asked: list[tuple[str, date | None]] = []
        self.confirmations = 0  # app confirmations the bank waits for at login

    def accounts(self) -> list[BankAccount]:
        return self.offered

    def fetch(self, account: BankAccount, start: date | None) -> Fetched:
        self.asked.append((account.bank_id, start))
        booked = [
            t for t in self.bookings[account.bank_id] if start is None or t.booking_date >= start
        ]
        return Fetched(account, start, self.balances[account.bank_id], TODAY, booked)

    def connect(self, login, pin, product, confirm):
        @contextmanager
        def session():
            if pin != self.pin:
                raise bank.PinError("PIN wrong")
            for _ in range(self.confirmations):
                if not confirm.wait_for_app("Bitte bestätigen"):
                    raise bank.Cancelled("cancelled")
            yield self

        return session()


def link(account: str, bank_id: str = IBAN, kind: str = "giro", fetch_from=None):
    return BankAccountLink(
        bank_id=bank_id, product="Girokonto", kind=kind, account=account, fetch_from=fetch_from
    )


LOGIN = BankLogin(login_id="test")


def fetched(fake: FakeBank, links: list[BankAccountLink], session) -> list:
    return [
        (
            LOGIN,
            lk,
            fake.fetch(
                next(a for a in fake.offered if a.bank_id == lk.bank_id),
                fetch.fetch_start(session, lk),
            ),
        )
        for lk in links
    ]


def balance(session, name: str) -> int:
    return next(b.balance_cents for b in account_balances(session) if b.name == name)


# -- service ---------------------------------------------------------------------------


def test_fetch_after_statements(app, statement_pdf):
    """The cut-over follows the last statement; bookings before it are left out; a second
    fetch finds the same bookings again and adds nothing."""
    fake = FakeBank()
    with app.session() as s:
        import_service.import_file(s, app.settings, statement_pdf)  # March, 3,555.87 EUR
        account = s.scalar(select(Account).where(Account.iban == IBAN))
        cut = fetch.suggested_fetch_from(s, account.id)
    assert cut == date(2026, 4, 1)
    girokonto = link("ING Girokonto", fetch_from=cut)
    fake.bookings[IBAN] = [
        tx(date(2026, 3, 31), -1),  # covered by the statement, the bank still lists it
        tx(date(2026, 4, 2), -5000),
        tx(date(2026, 4, 3), -909, "VISA PAYPAL *VINTED"),
        tx(date(2026, 4, 3), -909, "VISA PAYPAL *VINTED"),  # identical twice: both are kept
    ]
    fake.balances[IBAN] = 355587 - 5000 - 1818
    with app.session() as s:
        items = fetched(fake, [girokonto], s)
        result = fetch.import_fetched(s, app.settings, items, now=datetime(2026, 4, 20, 9, 0))
    assert fake.asked == [(IBAN, date(2026, 4, 1))]
    assert result.added == 3
    assert result.balance_differences == []
    with app.session() as s:
        assert balance(s, "ING Girokonto") == fake.balances[IBAN]
        batch = s.scalar(select(ImportBatch).where(ImportBatch.importer == "fints"))
        assert (batch.period_start, batch.period_end) == (date(2026, 4, 1), TODAY)
        assert batch.source_file == "FinTS ING 2026-04-20 09:00"
        # no gap between the statement and the fetch
        giro = next(b for b in account_balances(s) if b.name == "ING Girokonto")
        assert giro.gaps == []

    # again, with the overlap: the same bookings are recognised, no new history entry
    with app.session() as s:
        assert fetch.fetch_start(s, girokonto) == date(2026, 4, 10)  # last day - 10
        result = fetch.import_fetched(
            s, app.settings, [(LOGIN, girokonto, fake.fetch(fake.offered[0], cut))]
        )
        assert (result.added, result.accounts[0].duplicates) == (0, 3)
        assert s.scalar(select(func.count()).select_from(ImportBatch)) == 2


def test_balance_difference_is_reported(app):
    fake = FakeBank()
    fake.bookings[IBAN] = [tx(date(2026, 4, 2), -5000)]
    fake.balances[IBAN] = 10000
    with app.session() as s:
        first = fetch.import_fetched(s, app.settings, fetched(fake, [link("ING Girokonto")], s))
    # no statement before: the account starts with bank balance - fetched bookings
    assert first.balance_differences == []
    fake.bookings[IBAN].append(tx(date(2026, 4, 15), -2000))
    fake.balances[IBAN] = 10000 - 2000 - 100  # a booking of 1 EUR the bank did not list
    with app.session() as s:
        second = fetch.import_fetched(s, app.settings, fetched(fake, [link("ING Girokonto")], s))
    assert second.added == 1
    assert [r.balance_difference_cents for r in second.balance_differences] == [-100]


def test_new_account_starts_with_the_bank_balance(app):
    fake = FakeBank()
    extra = link("ING Extra-Konto 2", "DE99999999999999999999", "savings")
    fake.bookings[extra.bank_id] = [tx(date(2025, 12, 1), 50000, "Zins", "Zinsen")]
    fake.balances[extra.bank_id] = 1_250_000
    with app.session() as s:
        result = fetch.import_fetched(s, app.settings, fetched(fake, [extra], s))
    assert fake.asked == [(extra.bank_id, None)]  # as far back as the bank allows
    assert result.added == 1
    with app.session() as s:
        account = s.scalar(select(Account).where(Account.name == "ING Extra-Konto 2"))
        assert (account.kind.value, account.iban, account.on_budget) == (
            "savings",
            extra.bank_id,
            True,
        )
        assert balance(s, "ING Extra-Konto 2") == 1_250_000
        # PDF statements of this account are cut over at the first fetched day
        assert fetch.cutovers(s, settings_with([extra], app)) == {
            account.id: (date(2025, 12, 1), TODAY)
        }


def test_new_account_without_bookings_gets_its_balance(app):
    fake = FakeBank()
    extra = link("ING Extra-Konto 3", "DE99999999999999999999", "savings")
    fake.balances[extra.bank_id] = 777
    with app.session() as s:
        result = fetch.import_fetched(s, app.settings, fetched(fake, [extra], s))
        assert result.added == 0
        assert balance(s, "ING Extra-Konto 3") == 777
        # nothing new later: no further entries in the history
        fetch.import_fetched(s, app.settings, fetched(fake, [extra], s))
        assert s.scalar(select(func.count()).select_from(ImportBatch)) == 1


def test_undo_a_fetch_fetches_again(app):
    fake = FakeBank()
    fake.bookings[IBAN] = [tx(date(2026, 4, 2), -5000)]
    fake.balances[IBAN] = -5000
    giro = link("ING Girokonto", fetch_from=date(2026, 4, 1))
    with app.session() as s:
        result = fetch.import_fetched(s, app.settings, fetched(fake, [giro], s))
        import_service.delete_batch(s, app.settings, result.batch_ids[0], retrain=False)
        assert fetch.fetch_start(s, giro) == date(2026, 4, 1)
        again = fetch.import_fetched(s, app.settings, fetched(fake, [giro], s))
    assert again.added == 1


def test_fetched_bookings_are_categorized(app):
    fake = FakeBank()
    fake.bookings[IBAN] = [
        tx(date(2026, 4, 2), -5000, "Hausverwaltung Muster", "Hausgeld Musterstrasse 1")
    ]
    with app.session() as s:
        result = fetch.import_fetched(s, app.settings, fetched(fake, [link("ING Girokonto")], s))
    assert sum(result.categorization.counts.values()) == 1


def settings_with(links: list[BankAccountLink], app):
    app.settings.app.fints = FintsConfig(logins=[LOGIN.model_copy(update={"accounts": links})])
    return app.settings


def test_pdf_import_leaves_out_fetched_days(app, statement_pdf):
    with app.session() as s:
        account = s.scalar(select(Account).where(Account.iban == IBAN))
        fetched_days = {account.id: (date(2026, 3, 10), date(2026, 3, 31))}
        p = import_service.preview(s, statement_pdf, cutovers=fetched_days)
    assert p.skipped_fetched == 3  # 10., 25. and 31.03.
    assert p.errors == []  # the balance check ran on the whole statement
    assert any("FinTS" in w for w in p.statement.warnings)
    with app.session() as s:
        p = import_service.preview(s, statement_pdf, cutovers=fetched_days)
        r = import_service.commit(s, app.settings, p, retrain=False)
    assert r.added == 5


def test_pdf_import_after_the_last_fetch_is_imported(app, statement_pdf):
    """Only fetched days are left out: bookings after the last fetch come from the PDF."""
    with app.session() as s:
        account = s.scalar(select(Account).where(Account.iban == IBAN))
        p = import_service.preview(
            s, statement_pdf, cutovers={account.id: (date(2026, 3, 4), date(2026, 3, 20))}
        )
    assert (p.skipped_fetched, p.new_count) == (2, 6)  # 05. and 10.03. left out


def test_pdf_import_on_fetched_days_only_is_recorded(app, statement_pdf):
    """A statement entirely on fetched days: nothing to import, the file is recorded (it is
    not offered again)."""
    with app.session() as s:
        account = s.scalar(select(Account).where(Account.iban == IBAN))
        p = import_service.preview(
            s, statement_pdf, cutovers={account.id: (date(2026, 3, 1), date(2026, 3, 31))}
        )
        assert (p.new_count, p.skipped_fetched, p.can_commit) == (0, 8, True)
        r = import_service.commit(s, app.settings, p, retrain=False)
        assert r.added == 0
    with app.session() as s:
        assert import_service.new_statement_files(s, statement_pdf.parent) == []


def test_overwrite_keeps_fetched_days(app, statement_pdf):
    with app.session() as s:
        import_service.import_file(s, app.settings, statement_pdf)
        account = s.scalar(select(Account).where(Account.iban == IBAN))
        march = (date(2026, 3, 1), date(2026, 3, 31))
        fetched_days = (date(2026, 3, 10), date(2026, 3, 31))
        assert len(import_service._existing_in_range(s, account, *march)) == 8
        assert len(import_service._existing_in_range(s, account, *march, fetched_days)) == 5


def test_linked_but_not_fetched_account_imports_everything(app, statement_pdf):
    """Issue #20: a cut-over date alone does not leave out bookings; only fetched days do."""
    settings_with([link("ING Girokonto", fetch_from=date(2026, 3, 25))], app)
    with app.session() as s:
        assert fetch.cutovers(s, app.settings) == {}
        r = import_service.import_file(s, app.settings, statement_pdf)
    assert r.added == 8


def test_import_file_uses_the_fetched_days(app, statement_pdf):
    fake = FakeBank()
    giro = link("ING Girokonto", fetch_from=date(2026, 3, 25))
    fake.bookings[IBAN] = [tx(date(2026, 3, 26), -100)]
    with app.session() as s:
        fetch.import_fetched(s, settings_with([giro], app), fetched(fake, [giro], s))
        account = s.scalar(select(Account).where(Account.iban == IBAN))
        assert fetch.cutovers(s, app.settings) == {account.id: (date(2026, 3, 25), TODAY)}
        r = import_service.import_file(s, app.settings, statement_pdf)
    assert r.added == 6  # 25. and 31.03. are fetched


def test_fetch_replaces_statement_bookings_after_the_cutover(app, statement_pdf):
    """Issue #20: days after the last fetch came from a PDF; the next fetch delivers them
    with other texts (other fingerprints) and replaces them, keeping manual work."""
    from hiro_budget.db.models import Category, Transaction
    from hiro_budget.services import transactions as txs

    giro = link("ING Girokonto", fetch_from=date(2026, 3, 25))
    settings_with([giro], app)
    with app.session() as s:
        import_service.import_file(s, app.settings, statement_pdf)
        salary = s.scalar(select(Transaction).where(Transaction.amount_cents == 400000))
        category = s.scalars(select(Category)).first()
        txs.set_category(s, salary.id, category.id)
        salary.notes = "März"
    fake = FakeBank()
    fake.bookings[IBAN] = [
        tx(date(2026, 3, 25), 400000, "Beispiel AG", "Gehalt 03/2026 Mandat M-1"),
        tx(date(2026, 3, 31), -100000, "Musterfrau", "Miete Maerz Referenz R-1"),
        tx(date(2026, 4, 2), -5000),
    ]
    fake.balances[IBAN] = 355587 - 5000
    with app.session() as s:
        result = fetch.import_fetched(s, app.settings, fetched(fake, [giro], s), retrain=False)
    assert (result.accounts[0].added, result.accounts[0].replaced) == (3, 2)
    assert result.balance_differences == []
    with app.session() as s:
        account = s.scalar(select(Account).where(Account.iban == IBAN))
        stored = s.scalar(
            select(func.count())
            .select_from(Transaction)
            .where(Transaction.account_id == account.id)
        )
        assert stored == 9  # 8 from the statement, 2 of them now from FinTS, + 1 new
        salary = s.scalar(select(Transaction).where(Transaction.amount_cents == 400000))
        assert salary.batch.importer == "fints"
        assert salary.notes == "März"
        assert [sp.category_id for sp in salary.splits] == [category.id]
        assert salary.is_manual
        # the statement stays recorded with its other bookings
        assert import_service.new_statement_files(s, statement_pdf.parent) == []


# -- configuration ------------------------------------------------------------------------


def test_save_fints_round_trip_and_account_rename(app):
    fints = FintsConfig(
        logins=[
            BankLogin(
                login_id="123456",
                accounts=[link("ING Girokonto", fetch_from=date(2026, 4, 1))],
            )
        ]
    )
    config_edit.save_fints(app.settings, fints)
    text = app.settings.config_file.read_text("utf-8")
    assert "pin" not in text.lower()
    loaded = load_settings(app.settings.config_dir).app.fints
    assert loaded == fints
    accounts = [
        OwnAccount(name="Giro", kind="giro", iban=IBAN) if a.name == "ING Girokonto" else a
        for a in app.settings.app.own_accounts
    ]
    config_edit.save_accounts(app.settings, accounts, {"ING Girokonto": "Giro"})
    assert load_settings(app.settings.config_dir).app.fints.logins[0].accounts[0].account == "Giro"


# -- FinTS conversion -----------------------------------------------------------------------


def test_mt940_booking_as_on_the_statement():
    from hiro_budget.plugin_api import enricher_for
    from hiro_budget.services.bank.fints_client import kind_of, to_parsed

    t = to_parsed(
        {
            "entry_date": date(2026, 4, 2),
            "date": date(2026, 4, 1),
            "amount": Decimal("-23.45"),
            "posting_text": "Lastschrifteinzug",
            "applicant_name": "VISA  REWE\nMUSTERSTADT",
            "purpose": "NR XXXX 1111 MUSTERSTADT DE  KAUFUMSATZ 31.03 23.45",
            "end_to_end_reference": "NONREF",
            "additional_position_reference": "M-1",
        },
        enricher_for(bank_code="50010517"),  # ING card texts (plugin ing_common)
    )
    assert (t.booking_date, t.value_date, t.amount_cents) == (
        date(2026, 4, 2),
        date(2026, 4, 1),
        -2345,
    )
    assert t.booking_type == "Lastschrift"
    assert t.counterparty == "VISA REWE MUSTERSTADT"
    assert t.purpose == "NR XXXX 1111 MUSTERSTADT DE KAUFUMSATZ 31.03 23.45"
    assert (t.merchant, t.card_purchase_date) == ("REWE MUSTERSTADT", date(2026, 3, 31))
    assert (t.reference, t.mandate_ref) == (None, "M-1")
    assert [kind_of(n) for n in (1, 10, 30, 40, 50, None)] == [
        "giro",
        "savings",
        "depot",
        "loan",
        "credit_card",
        "giro",
    ]
    products = ["Girokonto", "Extra-Konto", "Direkt-Depot", "VISA Card", "Ratenkredit"]
    assert [kind_of(None, p) for p in products] == [
        "giro",
        "savings",
        "depot",
        "credit_card",
        "loan",
    ]


def test_app_confirmation_is_polled_until_confirmed():
    from fints.client import NeedTANResponse

    from hiro_budget.services.bank.fints_client import _Session

    class Pending(NeedTANResponse):
        def __init__(self) -> None:  # no bank message behind it
            self.decoupled = True
            self.challenge = "Bitte in der App bestätigen"

    class Client:
        polls = 0

        def send_tan(self, response, tan):
            self.polls += 1
            return Pending() if self.polls < 3 else "done"

    class Confirm:
        waits = 0

        def wait_for_app(self, challenge):
            self.waits += 1
            return True

        def tan(self, challenge):  # pragma: no cover - not asked for
            raise AssertionError

    confirm = Confirm()
    assert _Session(Client(), confirm).answer(Pending()) == "done"
    assert confirm.waits == 3

    confirm.wait_for_app = lambda challenge: False  # the user cancelled
    with pytest.raises(bank.Cancelled):
        _Session(Client(), confirm).answer(Pending())


def test_ing_accounts_without_type_and_depots_by_number():
    """ING sends no account type, and gives depots an IBAN while their statements carry the
    depot number (both seen in the real data, shapes only)."""
    from fints.models import SEPAAccount

    from hiro_budget.services.bank.fints_client import _Session

    giro = SEPAAccount("DE12500105170000000111", "INGDDEFFXXX", "0000000111", None, "50010517")
    depot = SEPAAccount("DE77500105178000001234", "INGDDEFFXXX", "8000001234", None, "50010517")
    extra = SEPAAccount("DE33500105170000002222", "INGDDEFFXXX", "0000002222", None, "50010517")

    class Client:
        def get_information(self):
            return {
                "accounts": [
                    {
                        "iban": a.iban,
                        "account_number": a.accountnumber,
                        "type": None,
                        "product_name": p,
                    }
                    for a, p in (
                        (giro, "Girokonto"),
                        (depot, "Direkt-Depot"),
                        (extra, "Extra-Konto"),
                    )
                ]
            }

        def get_sepa_accounts(self):
            return [giro, depot, extra]

    accounts = _Session(Client(), None).accounts()
    assert [(a.product, a.kind, a.bank_id) for a in accounts] == [
        ("Girokonto", "giro", giro.iban),
        ("Direkt-Depot", "depot", "8000001234"),
        ("Extra-Konto", "savings", extra.iban),
    ]


def test_find_account_by_iban_or_number(app):
    with app.session() as s:
        s.add(Account(name="ING Direkt-Depot", kind="depot", iban="8000001234"))
        s.flush()
        depot = BankAccount(
            "DE77500105178000001234",
            "Direkt-Depot",
            "depot",
            "DE77500105178000001234",
            account_number="8000001234",
        )
        assert fetch.find_account(s, depot).name == "ING Direkt-Depot"
        giro = BankAccount(IBAN, "Girokonto", "giro", IBAN, account_number="1234567890")
        assert fetch.find_account(s, giro).name == "ING Girokonto"
        other = BankAccount("DE99999999999999999999", "Extra-Konto", "savings")
        assert fetch.find_account(s, other) is None
