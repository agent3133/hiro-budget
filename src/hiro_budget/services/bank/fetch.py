"""Import what a bank session fetched: one import per account and fetch.

Each account has a cut-over date (``fetch_from``): statements cover the days before it,
FinTS the days from it on. A fetch asks for the days since the last fetch with a few days of
overlap; bookings already stored are recognised by their fingerprint (``dedupe_keys``: ING
gives no booking ID). Statements still deliver the days after the last fetch; the next fetch
replaces those bookings (an Umsatzanzeige lacks the Mandat/Referenz lines, so the
fingerprints differ). The import records the bank's balance as closing balance, so the
balance check and the statement gaps work as for PDF statements, and a new account starts
with the right balance (bank balance minus the fetched bookings).
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from hiro_budget.config import BUDGET_KINDS, BankAccountLink, BankLogin, Settings
from hiro_budget.db.models import (
    Account,
    AccountKind,
    ImportBatch,
    Split,
    SplitSource,
    Transaction,
)
from hiro_budget.services.accounts import account_balances
from hiro_budget.services.bank import BankAccount, Fetched
from hiro_budget.services.categorization.pipeline import (
    CategorizationStats,
    Categorizer,
    train_classifier,
)
from hiro_budget.services.import_service import (
    _resync_remaining_counterparts,
    _take_over_manual_entry,
    dedupe_keys,
    match_manual_entries,
)
from hiro_budget.services.linked_payments import LinkResult, link_waiting

log = logging.getLogger(__name__)

IMPORTER = "fints"
OVERLAP_DAYS = 10  # fetched again: bookings the bank adds late with an earlier date
FETCHED_KINDS = frozenset({"giro", "savings", "credit_card"})  # depots: holdings (#9)


def is_fetched(link: BankAccountLink) -> bool:
    return link.kind in FETCHED_KINDS


def _account(session: Session, name: str) -> Account | None:
    return session.scalar(select(Account).where(Account.name == name))


def find_account(session: Session, bank: BankAccount) -> Account | None:
    """The app account of a bank account: its IBAN or account number is stored as the
    account's IBAN (depots: the depot number of their statements)."""
    for key in dict.fromkeys(k for k in (bank.bank_id, bank.iban, bank.account_number) if k):
        account = session.scalar(select(Account).where(Account.iban == key))
        if account is not None:
            return account
    return None


def suggested_fetch_from(session: Session, account_id: int | None) -> date | None:
    """Day after the account's last imported statement; None (as far back as the bank
    allows) for an account without statements."""
    if account_id is None:
        return None
    last = session.scalar(
        select(func.max(ImportBatch.period_end)).where(
            ImportBatch.account_id == account_id,
            ImportBatch.valuation.is_(False),
            ImportBatch.importer != IMPORTER,
        )
    )
    return last + timedelta(days=1) if last else None


def _fints_range(session: Session, account_id: int) -> tuple[date | None, date | None]:
    return session.execute(
        select(func.min(ImportBatch.period_start), func.max(ImportBatch.period_end)).where(
            ImportBatch.account_id == account_id, ImportBatch.importer == IMPORTER
        )
    ).one()


def cutovers(session: Session, settings: Settings) -> dict[int, tuple[date, date]]:
    """Days fetched per FinTS for each fetched account (account id -> first, last day): PDF
    imports skip bookings on these days. Only fetched days count: an account that is linked
    but not fetched yet still takes its bookings from statements."""
    result: dict[int, tuple[date, date]] = {}
    for login in settings.app.fints.logins:
        for link in login.accounts:
            account = _account(session, link.account)
            if account is None or not is_fetched(link):
                continue
            first, last = _fints_range(session, account.id)
            if last is None:
                continue
            start = link.fetch_from or first
            if start <= last:
                result[account.id] = (start, last)
    return result


def _match_statement_bookings(
    session: Session, account: Account, items: list[tuple[int, date, int]], since: date
) -> dict[int, Transaction]:
    """Pair fetched bookings with bookings a statement delivered from ``since`` on.

    ``items`` are ``(index, booking_date, amount_cents)``; a statement booking matches with
    the same date and amount, each at most once.
    """
    if not items:
        return {}
    candidates: dict[tuple[date, int], list[Transaction]] = {}
    for c in session.scalars(
        select(Transaction)
        .join(ImportBatch, Transaction.batch_id == ImportBatch.id)
        .options(selectinload(Transaction.splits))
        .where(
            Transaction.account_id == account.id,
            ImportBatch.importer != IMPORTER,
            Transaction.booking_date >= since,
        )
        .order_by(Transaction.id)
    ):
        candidates.setdefault((c.booking_date, c.amount_cents), []).append(c)
    result: dict[int, Transaction] = {}
    for i, day, amount in items:
        if same := candidates.get((day, amount)):
            result[i] = same.pop(0)
    return result


def _replace_statement_booking(session: Session, tx: Transaction, old: Transaction) -> None:
    """Carry a statement booking's manual categorization and note over, then delete it."""
    tx.notes = old.notes
    if old.is_manual:
        tx.splits = [
            Split(
                amount_cents=s.amount_cents,
                category_id=s.category_id,
                transfer_account_id=s.transfer_account_id,
                memo=s.memo,
                source=SplitSource.MANUAL,
            )
            for s in old.splits
        ]
    session.delete(old)  # its counter-bookings go with it; categorization makes new ones


def fetch_start(session: Session, link: BankAccountLink) -> date | None:
    """First day to ask the bank for: since the last fetch (with overlap), not before the
    cut-over."""
    account = _account(session, link.account)
    last = _fints_range(session, account.id)[1] if account is not None else None
    if last is None:
        return link.fetch_from
    start = last - timedelta(days=OVERLAP_DAYS)
    return max(start, link.fetch_from) if link.fetch_from else start


@dataclass
class AccountResult:
    account: str
    added: int = 0
    duplicates: int = 0
    matched: int = 0  # manual entries replaced
    replaced: int = 0  # bookings from statements replaced
    bank_balance_cents: int | None = None
    app_balance_cents: int | None = None

    @property
    def balance_difference_cents(self) -> int | None:
        if self.bank_balance_cents is None or self.app_balance_cents is None:
            return None
        return self.bank_balance_cents - self.app_balance_cents


@dataclass
class FetchResult:
    accounts: list[AccountResult] = field(default_factory=list)
    categorization: CategorizationStats = field(default_factory=CategorizationStats)
    batch_ids: list[int] = field(default_factory=list)
    linking: LinkResult = field(default_factory=LinkResult)  # waiting PayPal payments

    @property
    def added(self) -> int:
        return sum(a.added for a in self.accounts)

    @property
    def balance_differences(self) -> list[AccountResult]:
        return [a for a in self.accounts if a.balance_difference_cents]


def _create_account(session: Session, login: BankLogin, link: BankAccountLink) -> Account:
    account = Account(
        name=link.account,
        kind=AccountKind(link.kind),
        institution=login.name,
        iban=link.bank_id,
        is_own=True,
        on_budget=link.kind in BUDGET_KINDS,
    )
    session.add(account)
    session.flush()
    log.info("created account %s", account.name)
    return account


def import_fetched(
    session: Session,
    settings: Settings,
    fetched: list[tuple[BankLogin, BankAccountLink, Fetched]],
    *,
    now: datetime | None = None,
    retrain: bool = True,
) -> FetchResult:
    """Store the fetched bookings and categorize them. Caller commits the session."""
    now = (now or datetime.now()).replace(microsecond=0)
    result = FetchResult()
    new_txs: list[Transaction] = []
    touched: list[Account] = []
    for login, link, f in fetched:
        account = _account(session, link.account) or _create_account(session, login, link)
        touched.append(account)
        txs = [
            t
            for t in f.transactions
            if link.fetch_from is None or t.booking_date >= link.fetch_from
        ]
        keys = dedupe_keys(link.bank_id, txs)
        stored = set(
            session.scalars(select(Transaction.dedupe_key).where(Transaction.dedupe_key.in_(keys)))
        )
        new = [i for i, k in enumerate(keys) if k not in stored]
        r = AccountResult(link.account, duplicates=len(keys) - len(new))
        r.bank_balance_cents = f.balance_cents
        result.accounts.append(r)
        has_batch = session.scalar(
            select(func.count())
            .select_from(ImportBatch)
            .where(ImportBatch.account_id == account.id)
        )
        if not new and has_batch:
            continue  # nothing new: no entry in the import history
        # before this fetch's batch exists: the first day FinTS covers
        since = link.fetch_from or _fints_range(session, account.id)[0]

        dates = [t.booking_date for t in txs]
        start = f.start or min(dates, default=f.balance_date or now.date())
        end = max([f.balance_date or now.date(), *dates])
        closing = f.balance_cents
        batch = ImportBatch(
            account=account,
            importer=IMPORTER,
            source_file=f"FinTS {login.name} {now:%Y-%m-%d %H:%M}",
            file_sha256=hashlib.sha256(
                f"fints|{link.bank_id}|{now.isoformat()}|{uuid.uuid4()}".encode()  # unique
            ).hexdigest(),
            statement_date=now.date(),
            period_start=start,
            period_end=end,
            opening_balance_cents=None
            if closing is None
            else closing - sum(t.amount_cents for t in txs),
            closing_balance_cents=closing,
        )
        session.add(batch)
        matches = match_manual_entries(
            session,
            account,
            [(i, txs[i].booking_date, txs[i].amount_cents) for i in new],
            settings.app.matching.days,
        )
        replaces = (
            {}
            if since is None
            else _match_statement_bookings(
                session,
                account,
                [(i, txs[i].booking_date, txs[i].amount_cents) for i in new if i not in matches],
                since,
            )
        )
        for i in new:
            t = txs[i]
            tx = Transaction(
                account=account,
                batch=batch,
                booking_date=t.booking_date,
                value_date=t.value_date,
                amount_cents=t.amount_cents,
                currency=account.currency or "EUR",
                booking_type=t.booking_type,
                counterparty=t.counterparty,
                purpose=t.purpose,
                mandate_ref=t.mandate_ref,
                reference=t.reference,
                merchant=t.merchant,
                card_purchase_date=t.card_purchase_date,
                raw_text=t.raw_text,
                dedupe_key=keys[i],
            )
            session.add(tx)
            if i in matches:
                _take_over_manual_entry(session, tx, matches[i])
            elif i in replaces:
                _replace_statement_booking(session, tx, replaces[i])
            new_txs.append(tx)
        r.added, r.matched, r.replaced = len(new), len(matches), len(replaces)
        session.flush()
        result.batch_ids.append(batch.id)

    if new_txs:
        result.categorization = Categorizer(session, settings).categorize(new_txs)
        result.linking = link_waiting(session, settings)
    for account in touched:
        _resync_remaining_counterparts(session, account, settings)
    session.flush()
    balances = {b.name: b.balance_cents for b in account_balances(session)}
    for r in result.accounts:
        r.app_balance_cents = balances.get(r.account)
    if retrain and new_txs and settings.app.ml.enabled:
        train_classifier(session, settings)
    log.info(
        "FinTS: %d new bookings in %d accounts; %s",
        result.added,
        len(result.batch_ids),
        result.categorization,
    )
    return result
