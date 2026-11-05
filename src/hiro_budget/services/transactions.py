"""Querying and manually editing bookings (used by the GUI, independent of Qt)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import Select, and_, exists, func, or_, select
from sqlalchemy.exc import NoResultFound
from sqlalchemy.orm import Session, aliased

from hiro_budget.config import Settings
from hiro_budget.db.models import (
    Account,
    Category,
    CategoryGroup,
    Split,
    SplitSource,
    Transaction,
)
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
    amount: str = ""  # an amount query ("17,99", "100..200", ">500"), apart from the text
    ids: tuple[int, ...] | None = None  # only these bookings (e.g. the hits of a rule)
    ids_label: str = ""  # what they are, shown above the list


_NUMBER = r"[+-]?\d[\d.,]*"
_RANGE = re.compile(rf"^({_NUMBER})\s*(?:\.\.|–|…)\s*({_NUMBER})$")
_BOUND = re.compile(rf"^(>=|<=|>|<)\s*({_NUMBER})$")
_SINGLE = re.compile(rf"^({_NUMBER})$")


@dataclass(frozen=True)
class AmountQuery:
    """Amounts a search text stands for, in cents (inclusive bounds, None: open).
    ``signed``: compare with the booking's amount, else with its absolute value."""

    low: int | None
    high: int | None
    signed: bool = False


def _cents(text: str) -> tuple[int, bool] | None:
    """ "17,99" / "17.99" / "1.234,56" / "-5" -> (cents, has decimals); None if no number."""
    sign = -1 if text.startswith("-") else 1
    text = text.lstrip("+-")
    if "," in text and "." in text:  # the last one is the decimal separator
        dec = "," if text.rfind(",") > text.rfind(".") else "."
        text = text.replace("." if dec == "," else ",", "").replace(dec, ".")
    elif "," in text:
        text = text.replace(",", ".")
    elif text.count(".") == 1 and len(text.rsplit(".", 1)[1]) == 3:
        text = text.replace(".", "")  # "1.234": thousands
    elif text.count(".") > 1:
        text = text.replace(".", "")  # "1.234.567"
    if text.count(".") > 1 or not re.fullmatch(r"\d+(\.\d{0,2})?", text):
        return None
    euros, _, frac = text.partition(".")
    return sign * (int(euros) * 100 + int((frac + "00")[:2])), bool(frac)


def parse_amount_query(text: str) -> AmountQuery | None:
    """An amount search: "17,99" (±17,99 €), "-17,99" (only that expense), "17" (17,00 to
    17,99 €), "100..200", ">500", "<=50"; "€" and spaces are ignored. None: no amount."""
    t = text.strip().replace("€", "").replace("EUR", "").replace(" ", "")
    if m := _RANGE.match(t):
        a, b = _cents(m[1]), _cents(m[2])
        if a is None or b is None:
            return None
        signed = m[1][0] in "+-" or m[2][0] in "+-"
        low, high = sorted((a[0], b[0] if b[1] else b[0] + (99 if b[0] >= 0 else 0)))
        return AmountQuery(low, high, signed)
    if m := _BOUND.match(t):
        v = _cents(m[2])
        if v is None:
            return None
        cents, op, signed = v[0], m[1], m[2][0] in "+-"
        if op == ">":
            return AmountQuery(cents + 1, None, signed)
        if op == ">=":
            return AmountQuery(cents, None, signed)
        if op == "<":
            return AmountQuery(None if signed else 0, cents - 1, signed)
        return AmountQuery(None if signed else 0, cents, signed)
    if m := _SINGLE.match(t):
        v = _cents(m[1])
        if v is None:
            return None
        cents, decimals = v
        if decimals:
            return AmountQuery(cents, cents, m[1][0] in "+-")
        whole = (cents, cents + 99) if cents >= 0 else (cents - 99, cents)
        return AmountQuery(*whole, m[1][0] in "+-")
    return None


def _amount_condition(q: AmountQuery):
    amount = Transaction.amount_cents if q.signed else func.abs(Transaction.amount_cents)
    parts = []
    if q.low is not None:
        parts.append(amount >= q.low)
    if q.high is not None:
        parts.append(amount <= q.high)
    return and_(*parts)


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


def leaves_budget(source: Account, target: Account | None) -> bool:
    """True for a transfer from a budget account to a tracking account."""
    return target is not None and source.on_budget and not target.on_budget


def needs_category(s: Split) -> bool:
    if s.category is not None:
        return False
    if s.transfer_account is None:
        return True
    return leaves_budget(s.transaction.account, s.transfer_account) and s.amount_cents < 0


def _apply_filter(q: Select, f: TxFilter) -> Select:
    if f.ids is not None:
        q = q.where(Transaction.id.in_(f.ids))
    if f.date_from:
        q = q.where(Transaction.booking_date >= f.date_from)
    if f.date_to:
        q = q.where(Transaction.booking_date <= f.date_to)
    if f.account_id:
        q = q.where(Transaction.account_id == f.account_id)
    if f.text.strip():
        like = f"%{f.text.strip()}%"
        matches = [
            Transaction.counterparty.ilike(like),
            Transaction.merchant.ilike(like),
            Transaction.purpose.ilike(like),
            Transaction.notes.ilike(like),
            Transaction.details.ilike(like),
        ]
        if (amount := parse_amount_query(f.text)) is not None:
            matches.append(_amount_condition(amount))  # "17,99" also finds the text
        q = q.where(or_(*matches))
    if f.amount.strip():
        amount = parse_amount_query(f.amount)
        if amount is None:
            raise ValueError(f"not an amount query: {f.amount!r}")
        q = q.where(_amount_condition(amount))
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


_TX_COLUMNS = (
    Transaction.id,
    Transaction.booking_date,
    Transaction.value_date,
    Transaction.account_id,
    Transaction.booking_type,
    Transaction.counterparty,
    Transaction.merchant,
    Transaction.purpose,
    Transaction.amount_cents,
    Transaction.raw_text,
    Transaction.mandate_ref,
    Transaction.reference,
    Transaction.notes,
    Transaction.batch_id,
    Transaction.details,
    Transaction.counterpart_of_id,
)


def list_transactions(
    session: Session, f: TxFilter | None = None, limit: int | None = None, offset: int = 0
) -> list[TransactionRow]:
    """Bookings of the filter, newest first (only the newest ``limit``, if given, after
    skipping ``offset``).

    Plain columns in three queries (bookings, their splits, their counter-bookings) and the
    few accounts and categories once, no ORM entities: ten years of bookings in a fraction
    of the time the entities took (#56)."""
    session.flush()  # pending edits, as the ORM query did by autoflush
    accounts = {
        a.id: (a.name, a.on_budget)
        for a in session.execute(select(Account.id, Account.name, Account.on_budget))
    }
    categories = {
        c.id: (c.key, c.name, c.group_name)
        for c in session.execute(
            select(
                Category.id, Category.key, Category.name, CategoryGroup.name.label("group_name")
            ).join(CategoryGroup, CategoryGroup.id == Category.group_id)
        )
    }
    source = aliased(Transaction)
    q: Select = select(*_TX_COLUMNS, source.account_id.label("source_account_id")).outerjoin(
        source, source.id == Transaction.counterpart_of_id
    )
    q = _apply_filter(q, f or TxFilter())
    q = q.order_by(Transaction.booking_date.desc(), Transaction.id.desc())
    if limit is not None:
        q = q.limit(limit)
        if offset:
            q = q.offset(offset)
    tx_rows = session.execute(q).all()
    if not tx_rows:
        return []
    ids = q.with_only_columns(Transaction.id).order_by(None).scalar_subquery()
    if limit is not None:  # LIMIT needs its ORDER BY inside the subquery
        ids = q.with_only_columns(Transaction.id).scalar_subquery()

    splits: dict[int, list[SplitView]] = {}
    account_of = {r.id: r.account_id for r in tx_rows}
    for s in session.execute(
        select(
            Split.transaction_id,
            Split.category_id,
            Split.transfer_account_id,
            Split.amount_cents,
            Split.memo,
            Split.source,
            Split.rule_id,
            Split.confidence,
        )
        .where(Split.transaction_id.in_(ids))
        .order_by(Split.id)
    ):
        cat = categories.get(s.category_id) if s.category_id is not None else None
        target = accounts.get(s.transfer_account_id) if s.transfer_account_id else None
        if cat is not None:
            needs = False
        elif target is None:
            needs = True
        else:  # a transfer leaving the budget for a tracking account needs a category
            needs = accounts[account_of[s.transaction_id]][1] and not target[1]
            needs = needs and s.amount_cents < 0
        splits.setdefault(s.transaction_id, []).append(
            SplitView(
                category_id=s.category_id,
                category_key=cat[0] if cat else None,
                category_name=cat[1] if cat else None,
                group_name=cat[2] if cat else None,
                transfer_account_id=s.transfer_account_id,
                transfer_account_name=target[0] if target else None,
                amount_cents=s.amount_cents,
                memo=s.memo,
                source=s.source,
                rule_id=s.rule_id,
                confidence=s.confidence,
                needs_category=needs,
            )
        )
    counterparts: dict[int, list[tuple[int, str]]] = {}
    for c in session.execute(
        select(Transaction.counterpart_of_id, Transaction.id, Transaction.account_id)
        .where(Transaction.counterpart_of_id.in_(ids))
        .order_by(Transaction.id)
    ):
        counterparts.setdefault(c.counterpart_of_id, []).append((c.id, accounts[c.account_id][0]))

    return [
        TransactionRow(
            id=r.id,
            booking_date=r.booking_date,
            value_date=r.value_date,
            account_name=accounts[r.account_id][0],
            booking_type=r.booking_type,
            payee=r.merchant or r.counterparty,
            counterparty=r.counterparty,
            merchant=r.merchant,
            purpose=r.purpose,
            amount_cents=r.amount_cents,
            raw_text=r.raw_text,
            mandate_ref=r.mandate_ref,
            reference=r.reference,
            notes=r.notes,
            account_id=r.account_id,
            is_manual_entry=r.batch_id is None,
            counterpart_of=(
                accounts[r.source_account_id][0] if r.counterpart_of_id is not None else None
            ),
            details=r.details,
            counterpart_of_id=r.counterpart_of_id,
            counterparts=counterparts.get(r.id, []),
            splits=splits.get(r.id, []),
        )
        for r in tx_rows
    ]


def count_transactions(session: Session, f: TxFilter) -> int:
    """How many bookings the filter shows (without loading them)."""
    q = _apply_filter(select(func.count(Transaction.id)), f)
    return session.scalar(q) or 0


def get_row(session: Session, tx_id: int) -> TransactionRow:
    rows = list_transactions(session, TxFilter(ids=(tx_id,)))
    if not rows:
        raise NoResultFound(f"no booking {tx_id}")
    return rows[0]


def review_count(session: Session) -> int:
    """Bookings with a category suggested by the classifier (not yet confirmed)."""
    return session.execute(
        select(func.count(func.distinct(Split.transaction_id))).where(
            Split.source == SplitSource.ML
        )
    ).scalar_one()


# --- manual edits -------------------------------------------------------------------


HISTORY_LAST = 5  # earlier bookings the card lists
HISTORY_DAYS = 365  # count and average over this many days up to the booking
PRICE_STEADY = 3  # this many equal amounts before a different one: a price change


@dataclass
class PayeeBooking:
    id: int
    booking_date: date
    amount_cents: int


@dataclass
class PayeeHistory:
    """The other bookings of a booking's payee (same name, ignoring digits and case, as
    the payees report groups them), for the booking card (#50)."""

    name: str
    ids: list[int]  # all bookings of the payee, this one included
    year_count: int  # bookings in the 12 months up to this one, this one included
    year_average: int  # their average amount (cents, signed)
    last: list[PayeeBooking]  # the newest others, newest first
    previous_amount: int | None = None  # a steady amount before this one changed


def payee_history(session: Session, tx_id: int) -> PayeeHistory | None:
    """None when the booking has no payee name or is the payee's only booking."""
    from datetime import timedelta

    from hiro_budget.services.recurring import payee_key

    tx = session.get(Transaction, tx_id)
    if tx is None:
        return None
    name = tx.merchant or tx.counterparty or ""
    key = payee_key(name)
    if not key:
        return None
    like = f"%{max(key.split(), key=len)}%"  # narrows in SQL; the key decides below
    rows = session.execute(
        select(
            Transaction.id,
            Transaction.booking_date,
            Transaction.amount_cents,
            Transaction.merchant,
            Transaction.counterparty,
        )
        .where(or_(Transaction.merchant.ilike(like), Transaction.counterparty.ilike(like)))
        .order_by(Transaction.booking_date.desc(), Transaction.id.desc())
    ).all()
    same = [
        PayeeBooking(i, day, cents)
        for i, day, cents, merchant, counterparty in rows
        if payee_key(merchant or counterparty or "") == key
    ]
    others = [b for b in same if b.id != tx.id]
    if not others:
        return None
    start = tx.booking_date - timedelta(days=HISTORY_DAYS)
    year = [b for b in same if start < b.booking_date <= tx.booking_date]
    before = [b for b in others if (b.booking_date, b.id) < (tx.booking_date, tx.id)]
    steady = before[:PRICE_STEADY]
    previous = None
    if (
        len(steady) == PRICE_STEADY
        and len({b.amount_cents for b in steady}) == 1
        and steady[0].amount_cents != tx.amount_cents
    ):
        previous = steady[0].amount_cents
    return PayeeHistory(
        name=name,
        ids=[b.id for b in same],
        year_count=len(year),
        year_average=round(sum(b.amount_cents for b in year) / len(year)) if year else 0,
        last=others[:HISTORY_LAST],
        previous_amount=previous,
    )


@dataclass
class SplitInput:
    amount_cents: int
    category_id: int | None = None
    transfer_account_id: int | None = None
    memo: str | None = None


class SplitError(ValueError):
    pass


def _check_editable(tx: Transaction) -> None:
    if tx.counterpart_of is not None:
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
    session: Session, tx_id: int, category_id: int | None, days: int = DEFAULT_MATCH_DAYS
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
