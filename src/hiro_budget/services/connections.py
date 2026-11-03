"""Bank access through connector plugins (#82): connect, decide what to fetch, import it.

A login (``config.BankLogin``) names a connector (``importers.connectors``, e.g. the FinTS
plugin), the connector's settings and the bank's accounts with the app account each goes
to. Nothing here talks to a bank itself or touches Qt: the GUI runs ``connect`` on a worker
thread and hands what came back to ``import_fetched`` on the GUI thread.

Each account has a cut-over date (``fetch_from``): statements cover the days before it, the
connector the days from it on. A fetch asks for the days since the last fetch with a few
days of overlap; bookings already stored are recognised by their fingerprint
(``import_service.dedupe_keys``: ING gives no booking ID). Statements still deliver the days
after the last fetch; the next fetch replaces those bookings (an Umsatzanzeige lacks the
Mandat/Referenz lines, so the fingerprints differ). The import records the bank's balance as
closing balance, so the balance check and the statement gaps work as for PDF statements,
and a new account starts with the right balance (bank balance minus the fetched bookings).
All of that is the incremental mode of ``import_service``: every fetched account becomes an
import batch of its own, in the import history and undoable like a file.
"""

from __future__ import annotations

import logging
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from hiro_budget.config import BankAccountLink, BankLogin, Settings
from hiro_budget.db.models import Account, ImportBatch, Transaction
from hiro_budget.importers.base import ParsedStatement
from hiro_budget.importers.connectors import (
    Confirmation,
    Connector,
    ConnectorError,
    ConnectorSession,
    RemoteAccount,
    error_message,
)
from hiro_budget.services.accounts import account_balances
from hiro_budget.services.categorization.pipeline import (
    CategorizationStats,
    Categorizer,
    train_classifier,
)
from hiro_budget.services.import_service import (
    _resync_remaining_counterparts,
    commit,
    fetched_range,
    preview_fetched,
)
from hiro_budget.services.linked_payments import LinkResult, link_waiting

log = logging.getLogger(__name__)

OVERLAP_DAYS = 10  # fetched again: bookings the bank adds late with an earlier date
FETCHED_KINDS = frozenset({"giro", "savings", "credit_card"})  # depots: holdings (#9)


class SettingsError(ConnectorError):
    """A login's settings do not fit its connector (e.g. a server without https)."""


def is_fetched(link: BankAccountLink) -> bool:
    return link.kind in FETCHED_KINDS


# -- connectors ----------------------------------------------------------------------------


def connectors(settings: Settings | None) -> list[Connector]:
    """The connectors of the available plugins."""
    from hiro_budget.importers import plugins

    return plugins.of(settings).connectors()


def connector(settings: Settings | None, name: str) -> Connector:
    found = next((c for c in connectors(settings) if c.name == name), None)
    if found is None:
        raise SettingsError(f"no plugin provides the connector '{name}'")
    return found


def describe_errors(error: ValidationError) -> str:
    """One line per wrong setting, named by its field."""
    return "; ".join(
        f"{'.'.join(str(p) for p in e['loc']) or 'settings'}: {error_message(e)}"
        for e in error.errors()
    )


def login_settings(c: Connector, login: BankLogin):
    """The login's settings as the connector's model (raises SettingsError)."""
    try:
        return c.login_model.model_validate(login.settings)
    except ValidationError as e:
        raise SettingsError(describe_errors(e)) from e


def connector_options(c: Connector, settings: Settings | None, values: dict | None = None):
    """The settings shared by the connector's logins (None: it has none); ``values``
    instead of the saved ones (the settings dialog before saving)."""
    if c.options_model is None:
        return None
    if values is None:
        values = settings.app.bank_access.connectors.get(c.name, {}) if settings else {}
    try:
        return c.options_model.model_validate(values)
    except ValidationError as e:
        raise SettingsError(describe_errors(e)) from e


def connect(
    settings: Settings | None,
    login: BankLogin,
    secret: str,
    confirm: Confirmation,
    options: dict | None = None,
) -> AbstractContextManager[ConnectorSession]:
    """A session with the login's bank (``with connect(...) as bank: ...``); called on a
    worker thread. ``options``: the connector's shared settings instead of the saved ones.
    Tests replace it with a fake bank."""
    c = connector(settings, login.connector)
    return c.connect(
        login_settings(c, login), connector_options(c, settings, options), secret, confirm
    )


def title(settings: Settings | None, name: str) -> str:
    """The connector's title (its name if no plugin provides it any more)."""
    try:
        return connector(settings, name).title
    except SettingsError:
        return name


# -- what to fetch ---------------------------------------------------------------------------


def _account(session: Session, name: str) -> Account | None:
    return session.scalar(select(Account).where(Account.name == name))


def find_account(session: Session, bank: RemoteAccount) -> Account | None:
    """The app account of a bank account: its IBAN or account number is stored as the
    account's IBAN (depots: the depot number of their statements)."""
    for key in dict.fromkeys(k for k in (bank.bank_id, bank.iban, bank.account_number) if k):
        account = session.scalar(select(Account).where(Account.iban == key))
        if account is not None:
            return account
    return None


def suggested_fetch_from(session: Session, account_id: int | None, importer: str) -> date | None:
    """Day after the account's last imported statement (an import not by the connector
    ``importer``); None (as far back as the bank allows) for an account without
    statements."""
    if account_id is None:
        return None
    last = session.scalar(
        select(func.max(ImportBatch.period_end)).where(
            ImportBatch.account_id == account_id,
            ImportBatch.valuation.is_(False),
            ImportBatch.importer != importer,
        )
    )
    return last + timedelta(days=1) if last else None


def cutovers(session: Session, settings: Settings) -> dict[int, tuple[date, date]]:
    """Days fetched by a connector for each fetched account (account id -> first, last
    day): file imports skip bookings on these days. Only fetched days count: an account that
    is linked but not fetched yet still takes its bookings from statements."""
    result: dict[int, tuple[date, date]] = {}
    for login in settings.app.bank_access.logins:
        for link in login.accounts:
            account = _account(session, link.account)
            if account is None or not is_fetched(link):
                continue
            first, last = fetched_range(session, account.id, login.connector)
            if last is None or first is None:
                continue
            start = link.fetch_from or first
            if start <= last:
                result[account.id] = (start, last)
    return result


def fetch_start(session: Session, link: BankAccountLink, importer: str) -> date | None:
    """First day to ask the bank for: since the last fetch (with overlap), not before the
    cut-over."""
    account = _account(session, link.account)
    last = fetched_range(session, account.id, importer)[1] if account is not None else None
    if last is None:
        return link.fetch_from
    start = last - timedelta(days=OVERLAP_DAYS)
    return max(start, link.fetch_from) if link.fetch_from else start


# -- importing what was fetched ---------------------------------------------------------------


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


def import_fetched(
    session: Session,
    settings: Settings,
    fetched: list[tuple[BankLogin, BankAccountLink, ParsedStatement]],
    *,
    now: datetime | None = None,
    retrain: bool = True,
) -> FetchResult:
    """Store what was fetched (one import per account) and categorize it all at once.
    Caller commits the session."""
    now = (now or datetime.now()).replace(microsecond=0)
    result = FetchResult()
    new_txs: list[Transaction] = []
    touched: list[str] = []
    titles: dict[str, str] = {}
    for login, link, stmt in fetched:
        # the app's view of the account, whatever the connector reported
        stmt.incremental = True
        stmt.account_name = link.account
        stmt.account_kind = link.kind
        stmt.account_iban = link.bank_id
        stmt.institution = login.name
        if login.connector not in titles:
            titles[login.connector] = title(settings, login.connector)
        p = preview_fetched(
            session,
            stmt,
            fetch_from=link.fetch_from,
            source=f"{titles[login.connector]} {login.name} {now:%Y-%m-%d %H:%M}",
            now=now,
        )
        r = AccountResult(link.account, duplicates=p.duplicate_count)
        r.bank_balance_cents = stmt.closing_balance_cents
        result.accounts.append(r)
        touched.append(link.account)
        if not p.can_commit:
            continue  # nothing new: no entry in the import history
        done = commit(session, settings, p, retrain=False, categorize=False)
        r.added, r.matched, r.replaced = done.added, done.matched, done.replaced
        new_txs += done.transactions
        result.batch_ids.append(done.batch_id)

    if new_txs:
        result.categorization = Categorizer(session, settings).categorize(new_txs)
        result.linking = link_waiting(session, settings)
    for name in touched:
        if (account := _account(session, name)) is not None:
            _resync_remaining_counterparts(session, account, settings)
    session.flush()
    balances = {b.name: b.balance_cents for b in account_balances(session)}
    for r in result.accounts:
        r.app_balance_cents = balances.get(r.account)
    if retrain and new_txs and settings.app.ml.enabled:
        train_classifier(session, settings)
    log.info(
        "fetched: %d new bookings in %d accounts; %s",
        result.added,
        len(result.batch_ids),
        result.categorization,
    )
    return result
