"""Payments of a payment service (PayPal) that a bank booking paid for.

A CSV import stores them as ``LinkedPayment`` instead of bookings (see
``importers.csv_import``). ``link_waiting`` pairs every one not linked yet with a bank
booking: same amount, booked on the payment's day or up to ``LINK_DAYS`` later, its
counterparty or purpose matching the payment's ``bank_match``, each booking once. The bank
booking gets the payee and article as ``details`` and is categorized again (unless it was
categorized by hand). It runs after every import and FinTS fetch, so payments whose bank
booking comes later are linked then.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from hiro_budget.config import Settings
from hiro_budget.db.models import Account, AccountKind, LinkedPayment, Transaction

OVERDUE_DAYS = 30  # waiting longer than this: probably no bank booking will come
# The bank books a PayPal payment up to 10 days later (measured on a real export: 5 days
# link 88 % of the payments, 7 days 94 %, 10 days and more 94.4 %).
LINK_DAYS = 10


@dataclass
class LinkResult:
    linked: int = 0  # linked now
    waiting: int = 0  # still without bank booking
    overdue: int = 0  # of those: older than OVERDUE_DAYS
    oldest_waiting: date | None = None


def link_waiting(session: Session, settings: Settings, *, today: date | None = None) -> LinkResult:
    """Link waiting payments to bank bookings; caller commits the session."""
    waiting = list(
        session.scalars(
            select(LinkedPayment)
            .where(LinkedPayment.transaction_id.is_(None))
            .order_by(LinkedPayment.booking_date, LinkedPayment.id)
        )
    )
    result = LinkResult()
    if waiting:
        linked = _link(session, waiting, LINK_DAYS)
        result.linked = len(linked)
        if linked:
            _categorize(session, settings, linked)
    rest = [p for p in waiting if p.transaction is None]
    today = today or date.today()
    result.waiting = len(rest)
    result.overdue = sum((today - p.booking_date).days > OVERDUE_DAYS for p in rest)
    result.oldest_waiting = min((p.booking_date for p in rest), default=None)
    return result


def _link(session: Session, waiting: list[LinkedPayment], days: int) -> list[Transaction]:
    start = min(p.booking_date for p in waiting)
    end = max(p.booking_date for p in waiting) + timedelta(days=days)
    taken = set(
        session.scalars(
            select(LinkedPayment.transaction_id).where(LinkedPayment.transaction_id.is_not(None))
        )
    )
    candidates: dict[int, list[Transaction]] = defaultdict(list)
    for tx in session.scalars(
        select(Transaction)
        .join(Account, Transaction.account_id == Account.id)
        .options(selectinload(Transaction.splits), selectinload(Transaction.account))
        .where(
            Account.kind != AccountKind.WALLET,
            Transaction.counterpart_of_id.is_(None),
            Transaction.batch_id.is_not(None),  # imported bank bookings only
            Transaction.booking_date >= start,
            Transaction.booking_date <= end,
            Transaction.amount_cents.in_({p.amount_cents for p in waiting}),
        )
        .order_by(Transaction.booking_date, Transaction.id)
    ):
        if tx.id not in taken:
            candidates[tx.amount_cents].append(tx)
    linked: list[Transaction] = []
    for p in waiting:
        rx = re.compile(p.bank_match) if p.bank_match else None
        last = p.booking_date + timedelta(days=days)
        match = next(
            (
                tx
                for tx in candidates[p.amount_cents]
                if p.booking_date <= tx.booking_date <= last
                and (rx is None or rx.search(f"{tx.counterparty} {tx.purpose}"))
            ),
            None,
        )
        if match is None:
            continue
        candidates[p.amount_cents].remove(match)
        p.transaction = match
        match.details = p.details
        linked.append(match)
    session.flush()
    return linked


def _categorize(session: Session, settings: Settings, txs: list[Transaction]) -> None:
    from hiro_budget.services.categorization.pipeline import Categorizer

    Categorizer(session, settings).categorize(txs)  # keeps manual categorizations


def unlink_batch(session: Session, settings: Settings, batch_id: int) -> None:
    """Before an import's payments are deleted: their bank bookings lose the details."""
    txs = list(
        session.scalars(
            select(Transaction)
            .join(LinkedPayment, LinkedPayment.transaction_id == Transaction.id)
            .options(selectinload(Transaction.splits), selectinload(Transaction.account))
            .where(LinkedPayment.batch_id == batch_id)
        )
    )
    for tx in txs:
        tx.details = None
    for p in session.scalars(select(LinkedPayment).where(LinkedPayment.batch_id == batch_id)):
        session.delete(p)
    session.flush()
    if txs:
        _categorize(session, settings, txs)
