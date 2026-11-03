"""Account balances and account maintenance (rename, delete)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from hiro_budget.db.models import Account, AccountKind, Holding, ImportBatch, Transaction


@dataclass
class AccountBalance:
    account_id: int
    name: str
    kind: AccountKind
    institution: str | None
    iban: str | None
    balance_cents: int
    anchor: str  # see Anchor.kind
    anchor_date: date | None
    last_booking: date | None
    booking_count: int
    manual_count: int  # manual entries incl. transfer counter-bookings
    statement_balance_cents: int | None  # closing balance of the latest statement
    statement_date: date | None
    gaps: list[StatementGap] = field(default_factory=list)


@dataclass
class StatementLink:
    """What the gap check needs of a statement (stored import or one being previewed)."""

    source_file: str
    period_start: date
    period_end: date
    opening_cents: int
    closing_cents: int


@dataclass
class StatementGap:
    """Consecutive statements whose balances do not connect: statements are missing."""

    after: StatementLink  # statement before the gap
    before: StatementLink  # statement after the gap

    @property
    def difference_cents(self) -> int:
        """Sum of the bookings missing between the two statements."""
        return self.before.opening_cents - self.after.closing_cents


def statement_links(batches: list[ImportBatch]) -> list[StatementLink]:
    return [
        StatementLink(
            b.source_file,
            b.period_start,
            b.period_end,
            b.opening_balance_cents,
            b.closing_balance_cents,
        )
        for b in batches
        if not b.valuation
        and b.period_start is not None
        and b.period_end is not None
        and b.opening_balance_cents is not None
        and b.closing_balance_cents is not None
    ]


def statement_gaps(links: list[StatementLink]) -> list[StatementGap]:
    """Places where a statement's opening balance differs from the previous closing balance.

    Statements overlapping the previous one (e.g. an Umsatzanzeige and the Kontoauszug of
    the same month) are not compared with it.
    """
    gaps: list[StatementGap] = []
    previous: StatementLink | None = None
    for link in sorted(links, key=lambda x: (x.period_start, x.period_end)):
        if previous is not None and link.period_start > previous.period_end:
            if link.opening_cents != previous.closing_cents:
                gaps.append(StatementGap(previous, link))
        if previous is None or link.period_end >= previous.period_end:
            previous = link
    return gaps


@dataclass
class Anchor:
    # "opening" (configured), "statement" (first import), "valuation" (latest securities
    # account statement), "none"
    kind: str
    date: date | None  # bookings before this date are ignored
    base_cents: int  # balance at the start of ``date``

    def __iter__(self):
        return iter((self.kind, self.date, self.base_cents))


def _valuation_date(b: ImportBatch) -> date:
    """A valuation's date: always set (the import refuses a valuation without one)."""
    assert b.period_end is not None
    return b.period_end


def account_anchor(account: Account, batches: list[ImportBatch]) -> Anchor:
    """Starting point of an account's balance.

    The latest valuation (securities account statement: market value at the end of its
    date) unless a configured opening balance is newer, else the configured opening
    balance, else the opening balance of the earliest imported statement, else zero with
    all bookings counting.
    """
    valuation = max(
        (b for b in batches if b.valuation and b.closing_balance_cents is not None),
        key=lambda b: (b.period_end, b.id),
        default=None,
    )
    if valuation is not None and (
        account.opening_date is None or _valuation_date(valuation) >= account.opening_date
    ):
        assert valuation.closing_balance_cents is not None  # chosen for it
        return Anchor(
            "valuation",
            _valuation_date(valuation) + timedelta(days=1),
            valuation.closing_balance_cents,
        )
    if account.opening_balance_cents is not None and account.opening_date is not None:
        return Anchor("opening", account.opening_date, account.opening_balance_cents)
    first = min(
        (
            b
            for b in batches
            if not b.valuation and b.opening_balance_cents is not None and b.period_start
        ),
        key=lambda b: (b.period_start, b.id),
        default=None,
    )
    if first is not None:
        assert first.opening_balance_cents is not None  # chosen for it
        return Anchor("statement", first.period_start, first.opening_balance_cents)
    return Anchor("none", None, 0)


def anchors(session: Session) -> dict[int, Anchor]:
    batches: dict[int, list[ImportBatch]] = {}
    for b in session.scalars(select(ImportBatch)):
        batches.setdefault(b.account_id, []).append(b)
    return {
        a.id: account_anchor(a, batches.get(a.id, [])) for a in session.scalars(select(Account))
    }


def account_balances(session: Session) -> list[AccountBalance]:
    """Balance per account = anchor balance + bookings on/after the anchor date.

    The anchor is the latest valuation of a securities account, the configured opening
    balance, the opening balance of the account's earliest imported statement, or zero
    (all bookings); see ``account_anchor``.
    """
    result = []
    for acc in session.scalars(select(Account).order_by(Account.name)):
        batches = list(
            session.scalars(
                select(ImportBatch)
                .where(ImportBatch.account_id == acc.id)
                .order_by(ImportBatch.period_start, ImportBatch.id)
            )
        )
        anchor, anchor_date, base = account_anchor(acc, batches)
        q = select(func.coalesce(func.sum(Transaction.amount_cents), 0)).where(
            Transaction.account_id == acc.id
        )
        if anchor_date is not None:
            q = q.where(Transaction.booking_date >= anchor_date)
        total = session.scalar(q)
        last_booking, count, manual = session.execute(
            select(
                func.max(Transaction.booking_date),
                func.count(Transaction.id),
                func.count(Transaction.id).filter(Transaction.batch_id.is_(None)),
            ).where(Transaction.account_id == acc.id)
        ).one()
        latest = max(
            (b for b in batches if b.closing_balance_cents is not None),
            key=lambda b: (b.period_end or date.min, b.id),
            default=None,
        )
        result.append(
            AccountBalance(
                account_id=acc.id,
                name=acc.name,
                kind=acc.kind,
                institution=acc.institution,
                iban=acc.iban,
                balance_cents=base + total,
                anchor=anchor,
                anchor_date=anchor_date,
                last_booking=last_booking,
                booking_count=count,
                manual_count=manual,
                statement_balance_cents=latest.closing_balance_cents if latest else None,
                statement_date=latest.period_end if latest else None,
                gaps=statement_gaps(statement_links(batches)),
            )
        )
    return result


@dataclass
class Valuation:
    """Holdings of a securities account on a date (from an imported Depotauszug)."""

    batch_id: int
    date: date
    source_file: str
    total_cents: int
    holdings: list[Holding]


def valuations(session: Session, account_id: int) -> list[Valuation]:
    """Valuations of an account, newest first (holdings are detached, read-only)."""
    batches = session.scalars(
        select(ImportBatch)
        .options(selectinload(ImportBatch.holdings))
        .where(ImportBatch.account_id == account_id, ImportBatch.valuation.is_(True))
        .order_by(ImportBatch.period_end.desc(), ImportBatch.id.desc())
    )
    return [
        Valuation(
            batch_id=b.id,
            date=_valuation_date(b),
            source_file=b.source_file,
            total_cents=b.closing_balance_cents or 0,
            holdings=list(b.holdings),
        )
        for b in batches
    ]


ISIN_IN_TEXT = re.compile(r"ISIN ?([A-Z]{2}[A-Z0-9]{9}\d) ?(.*)")


@dataclass
class Trade:
    date: date
    amount_cents: int  # bought (+) or sold (-) for the depot


@dataclass
class Position:
    """One security of a securities account: latest valuation plus later trades."""

    isin: str | None  # None: other value (bookings without ISIN, rest of the statement)
    name: str
    statement_cents: int = 0  # value in the latest Depotauszug (0: bought later)
    trades: list[Trade] = field(default_factory=list)  # after the Depotauszug

    @property
    def value_cents(self) -> int:
        """Statement value plus purchases minus sales (at cost: no market changes)."""
        return self.statement_cents + sum(t.amount_cents for t in self.trades)


@dataclass
class Positions:
    statement_date: date
    positions: list[Position]

    @property
    def total_cents(self) -> int:
        return sum(p.value_cents for p in self.positions)


def positions(session: Session, account_id: int) -> Positions | None:
    """Value per security, adding up to the account balance; None without a valuation.

    Trades after the valuation are the account's bookings since then; the ISIN comes from
    the booking text or, for transfer counter-bookings, the text of the original booking
    ("WP-ABRECHNUNG ... Kauf ISIN IE00B4X9L533 HSBC MSCI WORLD UCITS ETF").
    """
    account = session.get_one(Account, account_id)
    if account.kind != AccountKind.DEPOT:
        return None  # e.g. loans also have valuations (balance statements), no securities
    batches = list(
        session.scalars(
            select(ImportBatch)
            .options(selectinload(ImportBatch.holdings))
            .where(ImportBatch.account_id == account_id)
        )
    )
    anchor = account_anchor(account, batches)
    if anchor.kind != "valuation" or anchor.date is None:
        return None
    valuation = max(
        (b for b in batches if b.valuation and b.period_end == anchor.date - timedelta(days=1)),
        key=lambda b: b.id,
    )
    # names as the Depotauszüge spell them, also for securities sold since
    names = {
        h.isin: h.name
        for b in sorted(batches, key=lambda b: (b.period_end or date.min, b.id))
        for h in b.holdings
        if h.isin
    }
    by_key: dict[str, Position] = {}
    for h in sorted(valuation.holdings, key=lambda h: h.sort):
        p = by_key.setdefault(h.isin or h.name, Position(h.isin, h.name))
        p.statement_cents += h.value_cents
    rest = (valuation.closing_balance_cents or 0) - sum(h.value_cents for h in valuation.holdings)
    other = Position(None, "", statement_cents=rest)

    txs = session.scalars(
        select(Transaction)
        .options(selectinload(Transaction.counterpart_of))
        .where(Transaction.account_id == account_id, Transaction.booking_date >= anchor.date)
        .order_by(Transaction.booking_date, Transaction.id)
    )
    for tx in txs:
        texts = [tx.purpose, tx.counterpart_of.purpose if tx.counterpart_of else None]
        found = next((m for t in texts if t for m in [ISIN_IN_TEXT.search(t)] if m), None)
        trade = Trade(tx.booking_date, tx.amount_cents)
        if found is None:
            other.trades.append(trade)
            continue
        isin = found.group(1)
        name = names.get(isin) or found.group(2).strip() or isin
        by_key.setdefault(isin, Position(isin, name)).trades.append(trade)
    result = list(by_key.values())
    if other.statement_cents or other.trades:
        result.append(other)
    return Positions(_valuation_date(valuation), result)


class AccountError(ValueError):
    pass


def rename_account(session: Session, old: str, new: str) -> None:
    if old == new:
        return
    if session.scalar(select(Account).where(Account.name == new)):
        raise AccountError(f"an account named '{new}' already exists")
    acc = session.scalar(select(Account).where(Account.name == old))
    if acc is not None:
        acc.name = new
        session.flush()


def delete_account(session: Session, name: str) -> None:
    """Delete an account without bookings (accounts with bookings cannot be deleted)."""
    acc = session.scalar(select(Account).where(Account.name == name))
    if acc is None:
        return
    used = session.scalar(
        select(func.count()).select_from(Transaction).where(Transaction.account_id == acc.id)
    )
    if used:
        raise AccountError(
            f"account '{name}' has {used} bookings and cannot be deleted; "
            "delete its imports and manual bookings first"
        )
    session.delete(acc)
    session.flush()
