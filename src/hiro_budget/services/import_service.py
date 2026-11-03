"""Two-phase statement import: ``preview`` (parse + duplicate check) then ``commit``.

Every source goes through here (#82): a statement file (``preview``) and what a connector
fetched from a bank (``preview_fetched``, an ``incremental`` statement: bookings already
stored are skipped, bookings a statement delivered after the cut-over are replaced, and
the bank's balance becomes the closing balance). ``services.connections`` drives the
fetches.
"""

from __future__ import annotations

import enum
import hashlib
import logging
import re
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from hiro_budget.config import BUDGET_KINDS, Settings
from hiro_budget.db.models import (
    Account,
    AccountKind,
    Holding,
    ImportBatch,
    LinkedPayment,
    Split,
    SplitSource,
    Transaction,
)
from hiro_budget.importers.base import (
    FileImporter,
    ParsedLink,
    ParsedStatement,
    ParsedTransaction,
    find_importer,
)
from hiro_budget.services.accounts import (
    StatementGap,
    StatementLink,
    statement_gaps,
    statement_links,
)
from hiro_budget.services.categorization.pipeline import (
    CategorizationStats,
    Categorizer,
    train_classifier,
)
from hiro_budget.services.counterparts import sync_counterparts
from hiro_budget.services.linked_payments import LinkResult, link_waiting, unlink_batch

log = logging.getLogger(__name__)

_WS = re.compile(r"\s+")


class DuplicateFileError(Exception):
    pass


class BalanceError(Exception):
    pass


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


_hashes: dict[tuple[Path, int, int], str] = {}  # (path, size, mtime) -> sha256

STATEMENT_SUFFIXES = (".pdf", ".csv")


def is_statement_file(path: Path) -> bool:
    return path.suffix.lower() in STATEMENT_SUFFIXES


def new_statement_files(session: Session, folder: Path) -> list[Path]:
    """PDFs and CSVs in ``folder`` whose content was not imported yet (sorted by name)."""
    if not folder.is_dir():
        return []
    imported = set(session.scalars(select(ImportBatch.file_sha256)))
    found = []
    files = (p for p in folder.iterdir() if p.is_file() and is_statement_file(p))
    for path in sorted(files, key=lambda p: p.name.lower()):
        try:
            stat = path.stat()
            key = (path, stat.st_size, stat.st_mtime_ns)
            if key not in _hashes:
                _hashes[key] = file_sha256(path)
        except OSError:  # being written or locked
            continue
        if _hashes[key] not in imported:
            found.append(path)
    return found


def _norm(text: str | None) -> str:
    # Whitespace-insensitive: PDF text extraction is not always consistent with spaces.
    return _WS.sub("", text or "").lower()


def dedupe_keys(account_key: str, transactions: list[ParsedTransaction]) -> list[str]:
    """Stable keys per booking: the source's transaction ID if it has one, else a
    fingerprint of the booking; identical bookings get an occurrence index."""
    seen: Counter[str] = Counter()
    keys = []
    for t in transactions:
        if t.external_id:
            base = f"{account_key}|id|{t.external_id}"
        else:
            base = "|".join(
                [
                    account_key,
                    t.booking_date.isoformat(),
                    str(t.amount_cents),
                    _norm(t.booking_type),
                    _norm(t.counterparty),
                    _norm(t.purpose),
                ]
            )
        occurrence = seen[base]
        seen[base] += 1
        keys.append(hashlib.sha256(f"{base}|{occurrence}".encode()).hexdigest())
    return keys


@dataclass
class Overlap:
    """Existing bookings of the same account within the new statement's date range."""

    date_from: date
    date_to: date
    existing_count: int  # bookings already stored in the range
    removed_on_overwrite: int  # existing bookings the new file does not contain
    manual_kept: int  # manually edited bookings carried over on overwrite
    manual_lost: int  # manually edited bookings removed on overwrite
    files: list[str]  # earlier imports with bookings in the range


class OverlapMode(enum.StrEnum):
    ADD = "add"  # import only bookings that are not stored yet
    OVERWRITE = "overwrite"  # replace stored bookings in the statement's range


MATCH_DAYS_DEFAULT = 7


class OverlapError(Exception):
    pass


@dataclass
class ImportPreview:
    path: Path | None  # the statement file; None: fetched by a connector
    sha256: str
    statement: ParsedStatement
    account_name: str
    account_exists: bool
    file_already_imported: bool
    keys: list[str]
    duplicate_flags: list[bool]
    errors: list[str] = field(default_factory=list)
    overlap: Overlap | None = None
    # statement index -> manual entry that the imported booking will replace (add mode)
    matches: dict[int, Transaction] = field(default_factory=dict)
    # valuation: file of a stored valuation of the same account and date (replaced)
    replaces_valuation: str | None = None
    # balances not connecting to the stored statements before/after this one
    gaps: list[StatementGap] = field(default_factory=list)
    # days FinTS has fetched for the account (first, last): bookings on them were left out
    fetched_days: tuple[date, date] | None = None
    skipped_fetched: int = 0
    # payments a bank booking paid for (PayPal): stored as links, not as bookings
    links_new: list[ParsedLink] = field(default_factory=list)
    links_known: int = 0  # already stored by an earlier import
    # the source's name in the import history (default: the file name)
    source: str = ""
    # fetched: bookings a statement delivered from this day on are replaced (the cut-over)
    replace_since: date | None = None
    first_import: bool = False  # fetched: the account has no import yet

    @property
    def source_name(self) -> str:
        return self.source or (self.path.name if self.path is not None else "")

    @property
    def new_count(self) -> int:
        return self.duplicate_flags.count(False)

    @property
    def duplicate_count(self) -> int:
        return self.duplicate_flags.count(True)

    @property
    def can_commit(self) -> bool:
        if self.statement.incremental:
            # nothing new: no entry in the import history, except the account's first (its
            # balance)
            return not self.errors and (self.new_count > 0 or self.first_import)
        # With an overlap, overwriting is possible even if every booking is already stored.
        # Everything left out (FinTS has it): the file is still recorded as imported.
        return not self.errors and (
            self.new_count > 0
            or bool(self.links_new)
            or self.overlap is not None
            or self.statement.valuation
            or self.skipped_fetched > 0
        )


@dataclass
class ImportResult:
    path: Path | None
    batch_id: int
    added: int
    duplicates: int
    categorization: CategorizationStats
    replaced: int = 0  # stored bookings removed by an overwrite
    matched: int = 0  # manual entries replaced by imported bookings
    holdings: int = 0  # positions of an imported valuation
    links: int = 0  # payments stored as links to bank bookings
    linking: LinkResult = field(default_factory=LinkResult)  # waiting payments, all imports
    # the new bookings (for a caller that categorizes several imports at once)
    transactions: list[Transaction] = field(default_factory=list)


def _find_account(session: Session, stmt: ParsedStatement) -> Account | None:
    if stmt.account_iban:
        acc = session.scalar(select(Account).where(Account.iban == stmt.account_iban))
        if acc:
            return acc
    return session.scalar(select(Account).where(Account.name == stmt.account_name))


def _stored_by_date_amount(
    session: Session,
    account: Account,
    transactions: list[ParsedTransaction],
    keys: list[str],
    existing: set[str],
) -> list[bool]:
    """Duplicate flags that also count a stored booking with the same date and amount
    (imported from another source, e.g. a PDF whose texts differ); each stored booking
    counts once."""
    stored: Counter[tuple[date, int]] = Counter(
        (day, cents)
        for day, cents in session.execute(
            select(Transaction.booking_date, Transaction.amount_cents).where(
                Transaction.account_id == account.id,
                Transaction.batch_id.is_not(None),
                Transaction.booking_date >= min(t.booking_date for t in transactions),
                Transaction.booking_date <= max(t.booking_date for t in transactions),
            )
        )
    )
    flags = []
    for t, key in zip(transactions, keys, strict=True):
        slot = (t.booking_date, t.amount_cents)
        duplicate = key in existing or stored[slot] > 0
        if duplicate and stored[slot] > 0:
            stored[slot] -= 1
        flags.append(duplicate)
    return flags


def statement_range(stmt: ParsedStatement) -> tuple[date, date]:
    """Date range covered by a statement: its period, widened to all booking dates."""
    dates = [t.booking_date for t in stmt.transactions]
    start = min([d for d in (stmt.period_start, *dates) if d is not None])
    end = max([d for d in (stmt.period_end, *dates) if d is not None])
    return start, end


def _existing_in_range(
    session: Session,
    account: Account,
    start: date,
    end: date,
    skip: tuple[date, date] | None = None,
) -> list[Transaction]:
    """Imported bookings in the range, without those on the days in ``skip`` (fetched)."""
    query = (
        select(Transaction)
        .options(selectinload(Transaction.splits), selectinload(Transaction.batch))
        .where(
            Transaction.account_id == account.id,
            Transaction.batch_id.is_not(None),  # manual entries are never overwritten
            Transaction.booking_date >= start,
            Transaction.booking_date <= end,
        )
    )
    if skip is not None:
        query = query.where(~Transaction.booking_date.between(*skip))
    return list(session.scalars(query))


def match_manual_entries(
    session: Session, account: Account, items: list[tuple[int, date, int]], days: int
) -> dict[int, Transaction]:
    """Pair imported bookings with manual entries on the same account.

    ``items`` are ``(index, booking_date, amount_cents)`` of bookings about to be
    imported. A manual entry matches with the same amount and a date at most ``days``
    apart; closest dates are paired first, each manual entry at most once.
    """
    if not items or account.id is None:
        return {}
    start = min(d for _, d, _ in items) - timedelta(days=days)
    end = max(d for _, d, _ in items) + timedelta(days=days)
    candidates = list(
        session.scalars(
            select(Transaction)
            .options(selectinload(Transaction.splits), selectinload(Transaction.counterpart_of))
            .where(
                Transaction.account_id == account.id,
                Transaction.batch_id.is_(None),
                Transaction.booking_date >= start,
                Transaction.booking_date <= end,
            )
        )
    )
    pairs = sorted(
        (abs((c.booking_date - d).days), i, c.id, c)
        for i, d, amount in items
        for c in candidates
        if c.amount_cents == amount and abs((c.booking_date - d).days) <= days
    )
    result: dict[int, Transaction] = {}
    used: set[int] = set()
    for _diff, i, cid, c in pairs:
        if i not in result and cid not in used:
            result[i] = c
            used.add(cid)
    return result


def _take_over_manual_entry(session: Session, tx: Transaction, entry: Transaction) -> None:
    """Carry a matched manual entry's categorization and note over, then delete it."""
    if tx.notes is None:
        tx.notes = entry.notes
    if not tx.splits:
        if entry.counterpart_of is not None:
            # The entry was the counter-booking of a transfer: keep it a transfer.
            tx.splits = [
                Split(
                    amount_cents=tx.amount_cents,
                    transfer_account=entry.counterpart_of.account,
                    source=SplitSource.MANUAL,
                )
            ]
        elif entry.is_manual:
            tx.splits = [
                Split(
                    amount_cents=s.amount_cents,
                    category=s.category,
                    transfer_account=s.transfer_account,
                    memo=s.memo,
                    source=SplitSource.MANUAL,
                )
                for s in entry.splits
            ]
    if entry.counterpart_of is not None:
        entry.counterpart_of.counterparts.remove(entry)
    else:
        session.delete(entry)


def _find_overlap(
    session: Session,
    account: Account,
    start: date,
    end: date,
    new_keys: set[str],
    skip: tuple[date, date] | None = None,
) -> Overlap | None:
    existing = _existing_in_range(session, account, start, end, skip)
    if not existing:
        return None
    files = sorted({t.batch.source_file for t in existing if t.batch is not None})
    return Overlap(
        date_from=start,
        date_to=end,
        existing_count=len(existing),
        removed_on_overwrite=sum(1 for t in existing if t.dedupe_key not in new_keys),
        manual_kept=sum(1 for t in existing if t.is_manual and t.dedupe_key in new_keys),
        manual_lost=sum(1 for t in existing if t.is_manual and t.dedupe_key not in new_keys),
        files=files,
    )


@dataclass
class _ManualState:
    splits: list[tuple[int, int | None, int | None, str | None]]  # amount, cat, transfer, memo
    notes: str | None


def _remove_range(
    session: Session,
    account: Account,
    start: date,
    end: date,
    skip: tuple[date, date] | None = None,
) -> tuple[int, dict[str, _ManualState]]:
    """Delete stored bookings in the range (not on the days in ``skip``); return count and
    manual work by dedupe key."""
    saved: dict[str, _ManualState] = {}
    existing = _existing_in_range(session, account, start, end, skip)
    affected_batches = {t.batch_id for t in existing if t.batch_id is not None}
    for t in existing:
        manual = [
            (s.amount_cents, s.category_id, s.transfer_account_id, s.memo)
            for s in t.splits
            if t.is_manual
        ]
        if manual or t.notes:
            saved[t.dedupe_key] = _ManualState(manual, t.notes)
        session.delete(t)
    session.flush()
    # Imports whose bookings were all replaced disappear from the history.
    for batch_id in affected_batches:
        remaining = session.scalar(
            select(func.count()).select_from(Transaction).where(Transaction.batch_id == batch_id)
        )
        if not remaining:
            session.delete(session.get_one(ImportBatch, batch_id))
    session.flush()
    return len(existing), saved


def _restore_manual(tx: Transaction, state: _ManualState) -> None:
    tx.notes = state.notes
    if state.splits:
        tx.splits[:] = [
            Split(
                amount_cents=amount,
                category_id=cat,
                transfer_account_id=transfer,
                memo=memo,
                source=SplitSource.MANUAL,
            )
            for amount, cat, transfer, memo in state.splits
        ]


def statement_link(stmt: ParsedStatement, name: str) -> StatementLink | None:
    """The statement as link in the chain of statements (None: cannot be chained)."""
    if (
        stmt.valuation
        or stmt.period_start is None
        or stmt.period_end is None
        or stmt.opening_balance_cents is None
        or stmt.closing_balance_cents is None
    ):
        return None
    return StatementLink(
        name,
        stmt.period_start,
        stmt.period_end,
        stmt.opening_balance_cents,
        stmt.closing_balance_cents,
    )


def _gaps_with_stored(
    session: Session, account: Account, stmt: ParsedStatement, name: str
) -> list[StatementGap]:
    new = statement_link(stmt, name)
    if new is None:
        return []
    stored = statement_links(
        list(session.scalars(select(ImportBatch).where(ImportBatch.account_id == account.id)))
    )
    return [g for g in statement_gaps([*stored, new]) if new is g.after or new is g.before]


def plugin_importers(settings: Settings | None) -> list[FileImporter]:
    """File importers of the plugins (user plugins first), then their CSV formats. Without
    settings: the built-in plugins only."""
    from hiro_budget.importers import plugins

    own = settings.app.own_accounts if settings is not None else []
    return plugins.of(settings).file_importers(own)


def preview(
    session: Session,
    path: Path,
    *,
    match_days: int = MATCH_DAYS_DEFAULT,
    cutovers: dict[int, tuple[date, date]] | None = None,
    settings: Settings | None = None,
) -> ImportPreview:
    """Parse a statement and compare it with the stored bookings.

    ``cutovers`` (account id -> first and last day, see ``connections.cutovers``): days
    fetched by a connector; the statement's bookings on these days are left out, and an
    overwrite does not touch them. ``settings`` provide the configured CSV formats.
    """
    importer = find_importer(path, plugin_importers(settings))
    stmt = importer.parse(path)
    sha = file_sha256(path)
    account = _find_account(session, stmt)
    # checked on the complete statement, before bookings are left out
    balance_error = None
    if stmt.balance_checkable and not stmt.balance_ok:
        balance_error = (
            f"balance check failed (difference {stmt.balance_difference_cents / 100:.2f} EUR) "
            "- the statement was probably not parsed completely"
        )
    elif stmt.valuation and not stmt.balance_checkable:
        balance_error = "total value / closing balance not found - cannot be verified"
    fetched = (cutovers or {}).get(account.id) if account is not None else None
    skipped = 0
    if fetched is not None and not stmt.valuation:
        first, last = fetched
        kept = [t for t in stmt.transactions if not first <= t.booking_date <= last]
        skipped = len(stmt.transactions) - len(kept)
        if skipped:
            stmt.transactions = kept
            stmt.warnings.append(
                f"{skipped} bookings from {first:%d.%m.%Y} to {last:%d.%m.%Y} left out "
                "(fetched per FinTS)"
            )
    account_key = stmt.dedupe_scope or stmt.account_iban or stmt.account_name
    keys = dedupe_keys(account_key, stmt.transactions)
    existing = set(
        session.scalars(select(Transaction.dedupe_key).where(Transaction.dedupe_key.in_(keys)))
    )
    already = session.scalar(select(ImportBatch.id).where(ImportBatch.file_sha256 == sha))
    duplicates = [k in existing for k in keys]
    if stmt.match_by_date_amount and account is not None and stmt.transactions:
        duplicates = _stored_by_date_amount(session, account, stmt.transactions, keys, existing)
    p = ImportPreview(
        path=path,
        sha256=sha,
        statement=stmt,
        account_name=account.name if account else stmt.account_name,
        account_exists=account is not None,
        file_already_imported=already is not None,
        keys=keys,
        duplicate_flags=duplicates,
        fetched_days=fetched,
        skipped_fetched=skipped,
    )
    if stmt.links:
        known = (
            set(
                session.scalars(
                    select(LinkedPayment.external_id).where(
                        LinkedPayment.account_id == account.id,
                        LinkedPayment.external_id.in_([lk.external_id for lk in stmt.links]),
                    )
                )
            )
            if account is not None
            else set()
        )
        p.links_new = [lk for lk in stmt.links if lk.external_id not in known]
        p.links_known = len(stmt.links) - len(p.links_new)
    if p.file_already_imported:
        p.errors.append("this file has already been imported")
    elif stmt.valuation:
        if stmt.period_end is None:
            p.errors.append("valuation date not found")
        elif account is not None:
            p.replaces_valuation = session.scalar(
                select(ImportBatch.source_file).where(
                    ImportBatch.account_id == account.id,
                    ImportBatch.valuation.is_(True),
                    ImportBatch.period_end == stmt.period_end,
                )
            )
    elif account is not None and stmt.transactions:
        p.overlap = _find_overlap(
            session, account, *statement_range(stmt), set(keys), p.fetched_days
        )
    if account is not None and not p.file_already_imported and not stmt.valuation:
        p.gaps = _gaps_with_stored(session, account, stmt, path.name)
    if account is not None and not p.file_already_imported:
        p.matches = match_manual_entries(
            session,
            account,
            [
                (i, t.booking_date, t.amount_cents)
                for i, (t, dup) in enumerate(zip(stmt.transactions, p.duplicate_flags, strict=True))
                if not dup
            ],
            match_days,
        )
    if balance_error:
        p.errors.append(balance_error)
    return p


def fetched_range(
    session: Session, account_id: int, importer: str
) -> tuple[date | None, date | None]:
    """First and last day of the account's imports by ``importer`` (a connector)."""
    first, last = session.execute(
        select(func.min(ImportBatch.period_start), func.max(ImportBatch.period_end)).where(
            ImportBatch.account_id == account_id, ImportBatch.importer == importer
        )
    ).one()
    return first, last


def _account_named(session: Session, name: str) -> Account | None:
    return session.scalar(select(Account).where(Account.name == name))


def preview_fetched(
    session: Session,
    stmt: ParsedStatement,
    *,
    fetch_from: date | None,
    source: str,
    now: datetime,
) -> ImportPreview:
    """Compare what a connector fetched with the stored bookings (``stmt.incremental``).

    ``stmt.account_name`` is the app account it goes to (created on commit if missing),
    ``fetch_from`` its cut-over: earlier bookings are left out (statements cover them), and
    bookings a statement delivered from the cut-over on are replaced on commit. The bank's
    balance becomes the closing balance, the opening balance follows from the bookings.
    """
    account = _account_named(session, stmt.account_name)
    stmt.transactions = [
        t for t in stmt.transactions if fetch_from is None or t.booking_date >= fetch_from
    ]
    if account is not None:
        stmt.currency = account.currency or "EUR"
    account_key = stmt.dedupe_scope or stmt.account_iban or stmt.account_name
    keys = dedupe_keys(account_key, stmt.transactions)
    stored = set(
        session.scalars(select(Transaction.dedupe_key).where(Transaction.dedupe_key.in_(keys)))
    )
    first_import = account is None or not session.scalar(
        select(func.count()).select_from(ImportBatch).where(ImportBatch.account_id == account.id)
    )
    # before this fetch's batch exists: the first day the connector covers
    since = fetch_from
    if since is None and account is not None:
        since = fetched_range(session, account.id, stmt.importer)[0]
    dates = [t.booking_date for t in stmt.transactions]
    balance_date = stmt.period_end
    stmt.period_start = stmt.period_start or min(dates, default=balance_date or now.date())
    stmt.period_end = max([balance_date or now.date(), *dates])
    stmt.statement_date = now.date()
    closing = stmt.closing_balance_cents
    stmt.opening_balance_cents = None if closing is None else closing - stmt.total_cents
    return ImportPreview(
        path=None,
        sha256=hashlib.sha256(  # unique: every fetch is an import of its own
            f"{stmt.importer}|{stmt.account_iban}|{now.isoformat()}|{uuid.uuid4()}".encode()
        ).hexdigest(),
        statement=stmt,
        account_name=stmt.account_name,
        account_exists=account is not None,
        file_already_imported=False,
        keys=keys,
        duplicate_flags=[k in stored for k in keys],
        source=source,
        replace_since=since,
        first_import=first_import,
    )


def _match_statement_bookings(
    session: Session,
    account: Account,
    items: list[tuple[int, date, int]],
    since: date,
    importer: str,
) -> dict[int, Transaction]:
    """Pair fetched bookings with bookings a statement (any other importer) delivered from
    ``since`` on.

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
            ImportBatch.importer != importer,
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


def commit(
    session: Session,
    settings: Settings,
    p: ImportPreview,
    *,
    mode: OverlapMode | None = None,
    retrain: bool = True,
    categorize: bool = True,
) -> ImportResult:
    """Persist a preview's bookings and categorize them. Caller commits the session.

    If the preview overlaps stored bookings, ``mode`` must say how to proceed:
    ``ADD`` imports only bookings not stored yet, ``OVERWRITE`` replaces the stored
    bookings in the statement's range (manual work on identical bookings is kept). A
    fetched (incremental) statement always adds. ``categorize=False``: the caller
    categorizes ``ImportResult.transactions`` (several imports at once) and retrains.
    """
    name = p.source_name
    if p.file_already_imported:
        raise DuplicateFileError(f"{name} has already been imported")
    if p.errors:
        raise BalanceError("; ".join(p.errors))
    if p.overlap is not None and mode is None:
        raise OverlapError(
            f"{name} overlaps stored bookings "
            f"({p.overlap.date_from} - {p.overlap.date_to}); choose add or overwrite"
        )
    stmt = p.statement

    if stmt.incremental:
        account = _account_named(session, stmt.account_name)
    else:
        account = _find_account(session, stmt)
    if account is None:
        account = Account(
            name=stmt.account_name,
            kind=AccountKind(stmt.account_kind),
            institution=stmt.institution,
            iban=stmt.account_iban,
            currency=stmt.currency,
            is_own=True,
            on_budget=stmt.account_kind in BUDGET_KINDS,
        )
        session.add(account)
        session.flush()
        log.info("created account %s", account.name)

    batch = ImportBatch(
        account=account,
        importer=stmt.importer,
        source_file=name,
        file_sha256=p.sha256,
        statement_no=stmt.statement_no,
        statement_date=stmt.statement_date,
        period_start=stmt.period_start,
        period_end=stmt.period_end,
        opening_balance_cents=stmt.opening_balance_cents,
        closing_balance_cents=stmt.closing_balance_cents,
        valuation=stmt.valuation,
    )
    session.add(batch)
    if stmt.valuation:
        return _commit_valuation(session, p, account, batch)

    overwrite = p.overlap is not None and mode == OverlapMode.OVERWRITE and not stmt.incremental
    replaced, saved = 0, dict[str, _ManualState]()
    if overwrite:
        replaced, saved = _remove_range(session, account, *statement_range(stmt), p.fetched_days)

    to_insert = [i for i, dup in enumerate(p.duplicate_flags) if overwrite or not dup]
    matches = match_manual_entries(
        session,
        account,
        [
            (i, stmt.transactions[i].booking_date, stmt.transactions[i].amount_cents)
            for i in to_insert
        ],
        settings.app.matching.days,
    )
    # fetched: bookings a statement delivered after the cut-over (other texts, other keys)
    replaces: dict[int, Transaction] = {}
    if stmt.incremental and p.replace_since is not None:
        replaces = _match_statement_bookings(
            session,
            account,
            [
                (i, stmt.transactions[i].booking_date, stmt.transactions[i].amount_cents)
                for i in to_insert
                if i not in matches
            ],
            p.replace_since,
            stmt.importer,
        )
        replaced = len(replaces)

    new_txs: list[Transaction] = []
    for i in to_insert:
        t, key = stmt.transactions[i], p.keys[i]
        tx = Transaction(
            account=account,
            batch=batch,
            booking_date=t.booking_date,
            value_date=t.value_date,
            amount_cents=t.amount_cents,
            currency=stmt.currency,
            booking_type=t.booking_type,
            counterparty=t.counterparty,
            purpose=t.purpose,
            mandate_ref=t.mandate_ref,
            reference=t.reference,
            merchant=t.merchant,
            card_purchase_date=t.card_purchase_date,
            raw_text=t.raw_text,
            dedupe_key=key,
        )
        if key in saved:
            _restore_manual(tx, saved[key])
        session.add(tx)
        if i in matches:
            _take_over_manual_entry(session, tx, matches[i])
        elif i in replaces:
            _replace_statement_booking(session, tx, replaces[i])
        new_txs.append(tx)
    for lk in p.links_new:
        session.add(
            LinkedPayment(
                account=account,
                batch=batch,
                external_id=lk.external_id,
                booking_date=lk.booking_date,
                amount_cents=lk.amount_cents,
                payee=lk.payee,
                description=lk.description,
                bank_match=lk.bank_match,
            )
        )
    session.flush()
    skipped = 0 if overwrite else p.duplicate_count
    if not categorize:
        log.info(
            "%s: %d imported, %d skipped, %d replaced, %d manual entries matched",
            name,
            len(new_txs),
            skipped,
            replaced,
            len(matches),
        )
        return ImportResult(
            p.path,
            batch.id,
            len(new_txs),
            skipped,
            CategorizationStats(),
            replaced,
            matched=len(matches),
            transactions=new_txs,
        )

    stats = Categorizer(session, settings).categorize(new_txs)
    _resync_remaining_counterparts(session, account, settings)
    session.flush()
    linking = link_waiting(session, settings)
    if retrain and settings.app.ml.enabled:
        train_classifier(session, settings)
    log.info(
        "%s: %d imported, %d skipped, %d replaced, %d manual entries matched, "
        "%d payments via bank (%d linked, %d waiting); %s",
        name,
        len(new_txs),
        skipped,
        replaced,
        len(matches),
        len(p.links_new),
        linking.linked,
        linking.waiting,
        stats,
    )
    return ImportResult(
        p.path,
        batch.id,
        len(new_txs),
        skipped,
        stats,
        replaced,
        matched=len(matches),
        links=len(p.links_new),
        linking=linking,
        transactions=new_txs,
    )


def _resync_remaining_counterparts(session: Session, account: Account, settings: Settings) -> None:
    """Drop counter-bookings on ``account`` that an imported booking now mirrors.

    Manual-entry matching pairs by closest date, so with several equal transfers a
    counter-booking can be left over although an imported booking represents it.
    """
    sources = session.scalars(
        select(Transaction)
        .options(selectinload(Transaction.splits), selectinload(Transaction.counterparts))
        .where(
            Transaction.id.in_(
                select(Transaction.counterpart_of_id).where(
                    Transaction.account_id == account.id,
                    Transaction.counterpart_of_id.is_not(None),
                )
            )
        )
    )
    sync_counterparts(session, list(sources), settings.app.matching.days)


def _commit_valuation(
    session: Session, p: ImportPreview, account: Account, batch: ImportBatch
) -> ImportResult:
    """Store the holdings; a valuation of the same date replaces the stored one."""
    stmt = p.statement
    session.flush()  # the new batch needs its id to exclude it below
    old = session.scalars(
        select(ImportBatch).where(
            ImportBatch.account_id == account.id,
            ImportBatch.valuation.is_(True),
            ImportBatch.period_end == stmt.period_end,
            ImportBatch.id != batch.id,
        )
    ).all()
    for b in old:
        session.delete(b)
    batch.holdings = holdings = [
        Holding(
            sort=i,
            name=h.name,
            isin=h.isin,
            wkn=h.wkn,
            quantity=h.quantity,
            unit=h.unit,
            price=h.price,
            price_currency=h.price_currency,
            value_cents=h.value_cents,
        )
        for i, h in enumerate(stmt.holdings)
    ]
    session.flush()
    log.info(
        "%s: valuation of %s on %s: %d positions, %.2f EUR%s",
        p.source_name,
        account.name,
        stmt.period_end,
        len(stmt.holdings),
        (stmt.closing_balance_cents or 0) / 100,
        f" (replaces {old[0].source_file})" if old else "",
    )
    return ImportResult(
        p.path, batch.id, 0, 0, CategorizationStats(), replaced=len(old), holdings=len(holdings)
    )


def import_file(
    session: Session, settings: Settings, path: Path, mode: OverlapMode | None = None
) -> ImportResult:
    from hiro_budget.services.connections import cutovers

    p = preview(
        session,
        path,
        match_days=settings.app.matching.days,
        cutovers=cutovers(session, settings),
        settings=settings,
    )
    return commit(session, settings, p, mode=mode)


def categorization_preview(
    session: Session, settings: Settings, p: ImportPreview
) -> list[str | None]:
    """Category labels the new bookings would get, per statement booking (None = duplicate).

    Runs a full commit inside the given session and rolls it back; use a dedicated session.
    """
    if not p.can_commit:
        return [None] * len(p.statement.transactions)
    try:
        commit(session, settings, p, mode=OverlapMode.ADD, retrain=False)
        labels: dict[str, str] = {}
        for tx in session.scalars(select(Transaction).where(Transaction.dedupe_key.in_(p.keys))):
            parts = []
            for s in tx.splits:
                if s.category is not None:
                    parts.append(s.category.name)
                elif s.transfer_account is not None:
                    parts.append(f"↔ {s.transfer_account.name}")
            labels[tx.dedupe_key] = " + ".join(parts)
        return [
            None if dup else labels.get(k, "")
            for k, dup in zip(p.keys, p.duplicate_flags, strict=True)
        ]
    finally:
        session.rollback()


# --- import history -------------------------------------------------------------------


@dataclass
class BatchInfo:
    id: int
    source_file: str
    imported_at: datetime
    account_name: str
    statement_no: str | None
    period_start: date | None
    period_end: date | None
    booking_count: int
    manual_count: int  # bookings with manual categorization/split (lost on delete)
    valuation: bool = False
    holding_count: int = 0


def list_batches(session: Session) -> list[BatchInfo]:
    counts = dict(
        session.execute(
            select(Transaction.batch_id, func.count()).group_by(Transaction.batch_id)
        ).all()
    )
    manual = dict(
        session.execute(
            select(Transaction.batch_id, func.count(func.distinct(Transaction.id)))
            .join(Split, Split.transaction_id == Transaction.id)
            .where(Split.source == SplitSource.MANUAL)
            .group_by(Transaction.batch_id)
        ).all()
    )
    holdings = dict(
        session.execute(select(Holding.batch_id, func.count()).group_by(Holding.batch_id)).all()
    )
    batches = session.scalars(
        select(ImportBatch)
        .options(selectinload(ImportBatch.account))
        .order_by(ImportBatch.imported_at.desc(), ImportBatch.id.desc())
    )
    return [
        BatchInfo(
            id=b.id,
            source_file=b.source_file,
            # SQLite CURRENT_TIMESTAMP is UTC; show local time.
            imported_at=b.imported_at.replace(tzinfo=UTC).astimezone().replace(tzinfo=None),
            account_name=b.account.name,
            statement_no=b.statement_no,
            period_start=b.period_start,
            period_end=b.period_end,
            booking_count=counts.get(b.id, 0),
            manual_count=manual.get(b.id, 0),
            valuation=b.valuation,
            holding_count=holdings.get(b.id, 0),
        )
        for b in batches
    ]


def delete_batch(
    session: Session, settings: Settings, batch_id: int, *, retrain: bool = True
) -> int:
    """Delete an import and all its bookings (incl. splits). Returns the number of bookings.

    The file can be imported again afterwards. Caller commits the session.
    """
    batch = session.get_one(ImportBatch, batch_id)
    unlink_batch(session, settings, batch_id)
    txs = list(session.scalars(select(Transaction).where(Transaction.batch_id == batch_id)))
    for tx in txs:
        session.delete(tx)  # splits are deleted by the ORM cascade
    session.delete(batch)
    session.flush()
    if retrain and settings.app.ml.enabled:
        train_classifier(session, settings)
    log.info("deleted import %s (%s): %d bookings", batch_id, batch.source_file, len(txs))
    return len(txs)
