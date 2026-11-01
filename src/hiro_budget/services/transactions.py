"""Querying and manually editing bookings (used by the GUI, independent of Qt)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import Select, and_, exists, func, or_, select
from sqlalchemy.orm import Session, selectinload

from hiro_budget.config import Settings
from hiro_budget.db.models import Account, Category, Split, SplitSource, Transaction
from hiro_budget.services.categorization.pipeline import Categorizer
from hiro_budget.services.counterparts import (
    MANUAL_TYPE,
    new_manual_key,
    sync_counterparts,
)

DEFAULT_MATCH_DAYS = 7

# Special category filter values.
ALL = "all"
UNCATEGORIZED = "uncategorized"
REVIEW = "review"  # assigned by the ML classifier, not yet confirmed
TRANSFERS = "transfers"
GROUP_PREFIX = "group:"  # "group:<id>": any category of a category group


def group_filter(group_id: int) -> str:
    """Category filter value for all categories of a group."""
    return f"{GROUP_PREFIX}{group_id}"


@dataclass
class TxFilter:
    date_from: date | None = None
    date_to: date | None = None
    account_id: int | None = None
    category: str | int = ALL  # a constant above, group_filter(id) or a category id
    text: str = ""


@dataclass
class SplitView:
    category_id: int | None
    category_key: str | None
    category_name: str | None
    group_name: str | None
    transfer_account_id: int | None
    transfer_account_name: str | None
    amount_cents: int
    memo: str | None
    source: SplitSource
    rule_id: str | None
    confidence: float | None
    # Uncategorized: no category and no transfer, or an outflow from a budget account to a
    # tracking account without a budget category.
    needs_category: bool = False


@dataclass
class TransactionRow:
    id: int
    booking_date: date
    value_date: date | None
    account_name: str
    booking_type: str
    payee: str
    counterparty: str
    merchant: str | None
    purpose: str
    amount_cents: int
    raw_text: str
    mandate_ref: str | None
    reference: str | None
    notes: str | None
    account_id: int = 0
    is_manual_entry: bool = False  # entered by hand, not imported
    counterpart_of: str | None = None  # source account name of an automatic counter-booking
    details: str | None = None  # payee and article of a linked PayPal payment
    counterpart_of_id: int | None = None  # the booking it mirrors
    counterparts: list[tuple[int, str]] = field(default_factory=list)  # (id, account name)
    splits: list[SplitView] = field(default_factory=list)

    @property
    def is_counterpart(self) -> bool:
        return self.counterpart_of is not None

    @property
    def is_split(self) -> bool:
        return len(self.splits) > 1

    @property
    def is_manual(self) -> bool:
        return any(s.source == SplitSource.MANUAL for s in self.splits)

    @property
    def is_uncategorized(self) -> bool:
        return any(s.needs_category for s in self.splits)

    @property
    def needs_review(self) -> bool:
        return any(s.source == SplitSource.ML for s in self.splits)

    @property
    def single_category_id(self) -> int | None:
        if len(self.splits) == 1:
            return self.splits[0].category_id
        return None


def _split_view(s: Split) -> SplitView:
    return SplitView(
        category_id=s.category_id,
        category_key=s.category.key if s.category else None,
        category_name=s.category.name if s.category else None,
        group_name=s.category.group.name if s.category else None,
        transfer_account_id=s.transfer_account_id,
        transfer_account_name=s.transfer_account.name if s.transfer_account else None,
        amount_cents=s.amount_cents,
        memo=s.memo,
        source=s.source,
        rule_id=s.rule_id,
        confidence=s.confidence,
        needs_category=needs_category(s),
    )


def leaves_budget(source: Account, target: Account | None) -> bool:
    """True for a transfer from a budget account to a tracking account."""
    return target is not None and source.on_budget and not target.on_budget


def needs_category(s: Split) -> bool:
    if s.category is not None:
        return False
    if s.transfer_account is None:
        return True
    return leaves_budget(s.transaction.account, s.transfer_account) and s.amount_cents < 0


def _row(tx: Transaction) -> TransactionRow:
    return TransactionRow(
        id=tx.id,
        booking_date=tx.booking_date,
        value_date=tx.value_date,
        account_name=tx.account.name,
        booking_type=tx.booking_type,
        payee=tx.display_name,
        counterparty=tx.counterparty,
        merchant=tx.merchant,
        purpose=tx.purpose,
        amount_cents=tx.amount_cents,
        raw_text=tx.raw_text,
        mandate_ref=tx.mandate_ref,
        reference=tx.reference,
        notes=tx.notes,
        account_id=tx.account_id,
        is_manual_entry=tx.is_manual_entry,
        counterpart_of=tx.counterpart_of.account.name if tx.counterpart_of else None,
        details=tx.details,
        counterpart_of_id=tx.counterpart_of_id,
        counterparts=[(c.id, c.account.name) for c in tx.counterparts],
        splits=[_split_view(s) for s in tx.splits],
    )


def _apply_filter(q: Select, f: TxFilter) -> Select:
    if f.date_from:
        q = q.where(Transaction.booking_date >= f.date_from)
    if f.date_to:
        q = q.where(Transaction.booking_date <= f.date_to)
    if f.account_id:
        q = q.where(Transaction.account_id == f.account_id)
    if f.text.strip():
        like = f"%{f.text.strip()}%"
        q = q.where(
            or_(
                Transaction.counterparty.ilike(like),
                Transaction.merchant.ilike(like),
                Transaction.purpose.ilike(like),
                Transaction.notes.ilike(like),
                Transaction.details.ilike(like),
            )
        )
    split = select(Split.id).where(Split.transaction_id == Transaction.id)
    match f.category:
        case str() if f.category == UNCATEGORIZED:
            tracking = select(Account.id).where(Account.on_budget.is_(False))
            budget = select(Account.id).where(Account.on_budget.is_(True))
            q = q.where(
                exists(
                    split.where(
                        Split.category_id.is_(None),
                        or_(
                            Split.transfer_account_id.is_(None),
                            and_(
                                Split.transfer_account_id.in_(tracking),
                                Split.amount_cents < 0,
                                Transaction.account_id.in_(budget),
                            ),
                        ),
                    )
                )
            )
        case str() if f.category == REVIEW:
            q = q.where(exists(split.where(Split.source == SplitSource.ML)))
        case str() if f.category == TRANSFERS:
            q = q.where(exists(split.where(Split.transfer_account_id.is_not(None))))
        case str() if f.category.startswith(GROUP_PREFIX):
            group_id = int(f.category.removeprefix(GROUP_PREFIX))
            in_group = select(Category.id).where(Category.group_id == group_id)
            q = q.where(exists(split.where(Split.category_id.in_(in_group))))
        case int() as cat_id:
            q = q.where(exists(split.where(Split.category_id == cat_id)))
    return q


def list_transactions(
    session: Session, f: TxFilter | None = None, limit: int | None = None
) -> list[TransactionRow]:
    """Bookings of the filter, newest first (only the newest ``limit``, if given)."""
    q = select(Transaction).options(
        selectinload(Transaction.account),
        selectinload(Transaction.splits).selectinload(Split.category).selectinload(Category.group),
        selectinload(Transaction.splits).selectinload(Split.transfer_account),
        selectinload(Transaction.counterpart_of).selectinload(Transaction.account),
        selectinload(Transaction.counterparts).selectinload(Transaction.account),
    )
    q = _apply_filter(q, f or TxFilter())
    q = q.order_by(Transaction.booking_date.desc(), Transaction.id.desc())
    if limit is not None:
        q = q.limit(limit)
    return [_row(tx) for tx in session.scalars(q)]


def count_transactions(session: Session, f: TxFilter) -> int:
    """How many bookings the filter shows (without loading them)."""
    q = _apply_filter(select(func.count(Transaction.id)), f)
    return session.scalar(q) or 0


def get_row(session: Session, tx_id: int) -> TransactionRow:
    return _row(session.get_one(Transaction, tx_id))


def review_count(session: Session) -> int:
    """Bookings with a category suggested by the classifier (not yet confirmed)."""
    return session.scalar(
        select(func.count(func.distinct(Split.transaction_id))).where(
            Split.source == SplitSource.ML
        )
    )


# --- manual edits -------------------------------------------------------------------


@dataclass
class SplitInput:
    amount_cents: int
    category_id: int | None = None
    transfer_account_id: int | None = None
    memo: str | None = None


class SplitError(ValueError):
    pass


def _check_editable(tx: Transaction) -> None:
    if tx.is_counterpart:
        raise SplitError(
            "this is the counter-booking of a transfer; change the transfer on the "
            f"original booking in account '{tx.counterpart_of.account.name}'"
        )


def set_splits(
    session: Session, tx_id: int, parts: list[SplitInput], days: int = DEFAULT_MATCH_DAYS
) -> None:
    """Replace a booking's splits with manual ones; amounts must add up to the booking."""
    tx = session.get_one(Transaction, tx_id)
    _check_editable(tx)
    if not parts:
        raise SplitError("at least one split is required")
    total = sum(p.amount_cents for p in parts)
    if total != tx.amount_cents:
        raise SplitError(
            f"split amounts ({total / 100:.2f}) do not add up to the booking "
            f"({tx.amount_cents / 100:.2f})"
        )
    for p in parts:
        if p.category_id is not None and p.transfer_account_id is not None:
            target = session.get_one(Account, p.transfer_account_id)
            if not leaves_budget(tx.account, target):
                raise SplitError(
                    "a split can only have both a category and a transfer account when money "
                    "moves from a budget account to a tracking account (e.g. loan)"
                )
    tx.splits[:] = [
        Split(
            amount_cents=p.amount_cents,
            category=session.get(Category, p.category_id) if p.category_id else None,
            transfer_account=(
                session.get(Account, p.transfer_account_id) if p.transfer_account_id else None
            ),
            memo=p.memo or None,
            source=SplitSource.MANUAL,
        )
        for p in parts
    ]
    sync_counterparts(session, [tx], days)


def set_category(
    session: Session, tx_id: int, category_id: int, days: int = DEFAULT_MATCH_DAYS
) -> None:
    tx = session.get_one(Transaction, tx_id)
    transfer = None
    if len(tx.splits) == 1 and leaves_budget(tx.account, tx.splits[0].transfer_account):
        transfer = tx.splits[0].transfer_account_id  # keep the transfer, add the category
    part = SplitInput(tx.amount_cents, category_id=category_id, transfer_account_id=transfer)
    set_splits(session, tx_id, [part], days)


def mark_transfer(
    session: Session, tx_id: int, account_id: int, days: int = DEFAULT_MATCH_DAYS
) -> None:
    tx = session.get_one(Transaction, tx_id)
    if account_id == tx.account_id:
        raise SplitError("a booking cannot be a transfer to its own account")
    set_splits(session, tx_id, [SplitInput(tx.amount_cents, transfer_account_id=account_id)], days)


def accept_suggestion(session: Session, tx_id: int) -> None:
    """Confirm automatically assigned splits (e.g. ML) by turning them into manual ones."""
    tx = session.get_one(Transaction, tx_id)
    for s in tx.splits:
        if s.category is not None or s.transfer_account is not None:
            s.source = SplitSource.MANUAL
    session.flush()


def reset_to_automatic(session: Session, settings: Settings, tx_ids: list[int]) -> None:
    txs = [session.get_one(Transaction, i) for i in tx_ids]
    txs = [tx for tx in txs if not tx.is_counterpart]
    for tx in txs:
        tx.splits.clear()
    session.flush()
    Categorizer(session, settings).categorize(txs)
    session.flush()


def set_notes(session: Session, tx_id: int, notes: str) -> None:
    session.get_one(Transaction, tx_id).notes = notes.strip() or None


# --- manual bookings ------------------------------------------------------------------


@dataclass
class ManualEntry:
    account_id: int
    booking_date: date
    amount_cents: int  # negative = expense / money leaving the account
    payee: str
    purpose: str = ""
    category_id: int | None = None  # neither category nor transfer: categorize automatically
    transfer_account_id: int | None = None
    notes: str | None = None


def _manual_tx(session: Session, tx_id: int) -> Transaction:
    tx = session.get_one(Transaction, tx_id)
    _check_editable(tx)
    if not tx.is_manual_entry:
        raise SplitError("imported bookings cannot be edited or deleted, only categorized")
    return tx


def _apply_entry(session: Session, settings: Settings, tx: Transaction, e: ManualEntry) -> None:
    if e.amount_cents == 0:
        raise SplitError("the amount must not be zero")
    if e.transfer_account_id is not None and e.transfer_account_id == e.account_id:
        raise SplitError("a booking cannot be a transfer to its own account")
    amount_changed = tx.amount_cents is not None and tx.amount_cents != e.amount_cents
    account = session.get_one(Account, e.account_id)  # before tx joins the session
    tx.account = account
    tx.booking_date = tx.value_date = e.booking_date
    tx.amount_cents = e.amount_cents
    tx.counterparty = e.payee.strip()
    tx.purpose = e.purpose.strip()
    tx.notes = (e.notes or "").strip() or None
    session.add(tx)
    session.flush()
    days = settings.app.matching.days
    if e.category_id is not None or e.transfer_account_id is not None:
        part = SplitInput(e.amount_cents, e.category_id, e.transfer_account_id)
        set_splits(session, tx.id, [part], days)
    elif tx.is_manual and len(tx.splits) > 1 and not amount_changed:
        sync_counterparts(session, [tx], days)  # keep the user's split
    else:
        tx.splits.clear()
        session.flush()
        Categorizer(session, settings).categorize([tx])


def create_manual(session: Session, settings: Settings, e: ManualEntry) -> int:
    """Create a manual booking; returns its id."""
    tx = Transaction(
        booking_type=MANUAL_TYPE,
        dedupe_key=new_manual_key(),
        raw_text="",
    )
    _apply_entry(session, settings, tx, e)
    return tx.id


def update_manual(session: Session, settings: Settings, tx_id: int, e: ManualEntry) -> None:
    _apply_entry(session, settings, _manual_tx(session, tx_id), e)


def get_manual(session: Session, tx_id: int) -> ManualEntry:
    tx = _manual_tx(session, tx_id)
    single = tx.splits[0] if len(tx.splits) == 1 and tx.is_manual else None
    return ManualEntry(
        account_id=tx.account_id,
        booking_date=tx.booking_date,
        amount_cents=tx.amount_cents,
        payee=tx.counterparty,
        purpose=tx.purpose,
        category_id=single.category_id if single else None,
        transfer_account_id=single.transfer_account_id if single else None,
        notes=tx.notes,
    )


def delete_manual(session: Session, tx_id: int) -> None:
    """Delete a manual booking (its transfer counter-bookings are deleted with it)."""
    session.delete(_manual_tx(session, tx_id))
    session.flush()
