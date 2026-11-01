"""Linked counter-bookings for transfers between own accounts.

When a booking on account A has a split transferring ``x`` to own account B, account B
needs a booking of ``-x`` as well, otherwise B's balance is wrong. If B already has such
a booking (e.g. imported from B's own statement and marked as transfer back to A), it is
used as mirror and nothing is created. Otherwise a counter-booking is created on B and
linked to the source booking (``Transaction.counterpart_of``); it follows changes of the
transfer and is deleted with it.

When B's statement is imported later, the imported booking replaces the counter-booking
via the normal manual-entry matching (see ``import_service``).
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from hiro_budget.db.models import Account, Split, SplitSource, Transaction

COUNTERPART_TYPE = "Umbuchung"
MANUAL_TYPE = "Manuelle Buchung"


def new_manual_key() -> str:
    return f"manual:{uuid.uuid4().hex}"


class _Mirrors:
    """Bookings that may already be the other side of a transfer: the transfers on own
    accounts around the dates of ``transactions``, loaded with one query (instead of one
    per transfer, which took seconds at startup with ten years of bookings)."""

    def __init__(self, session: Session, transactions: list[Transaction], days: int) -> None:
        self.days = days
        self.by_account_amount: dict[tuple[int, int], list[Transaction]] = defaultdict(list)
        dates = [tx.booking_date for tx in transactions if tx.booking_date is not None]
        if not dates:
            return
        candidates = session.scalars(
            select(Transaction)
            .options(selectinload(Transaction.splits))
            .where(
                Transaction.counterpart_of_id.is_(None),
                Transaction.booking_date >= min(dates) - timedelta(days=days),
                Transaction.booking_date <= max(dates) + timedelta(days=days),
                Transaction.id.in_(
                    select(Split.transaction_id).where(Split.transfer_account_id.is_not(None))
                ),
            )
        )
        for tx in candidates:
            self.by_account_amount[(tx.account_id, tx.amount_cents)].append(tx)

    def find(self, source: Transaction, target: Account, amount_cents: int) -> Transaction | None:
        """A booking on ``target`` that already represents the other side of the transfer."""
        for tx in self.by_account_amount.get((target.id, amount_cents), ()):
            if abs((tx.booking_date - source.booking_date).days) <= self.days and any(
                s.transfer_account_id == source.account_id for s in tx.splits
            ):
                return tx
        return None


def sync_counterparts(session: Session, transactions: list[Transaction], days: int) -> None:
    """Create, update or delete counter-bookings so they match the transfer splits."""
    session.flush()
    mirrors = _Mirrors(session, transactions, days)
    for tx in transactions:
        if tx.is_counterpart or tx.id is None:
            continue
        desired: dict[int, int] = defaultdict(int)  # target account id -> amount on target
        targets: dict[int, Account] = {}
        for s in tx.splits:
            target = s.transfer_account
            if target is not None and target.id != tx.account_id:
                desired[target.id] -= s.amount_cents
                targets[target.id] = target
        existing = {c.account_id: c for c in tx.counterparts}

        for account_id, amount in desired.items():
            if amount == 0:
                continue
            current = existing.pop(account_id, None)
            mirror = mirrors.find(tx, targets[account_id], amount)
            if mirror is not None:
                if current is not None:
                    tx.counterparts.remove(current)
                continue
            if current is None:
                current = Transaction(
                    account=targets[account_id],
                    dedupe_key=new_manual_key(),
                    booking_type=COUNTERPART_TYPE,
                )
                tx.counterparts.append(current)
            current.booking_date = tx.booking_date
            current.value_date = tx.value_date
            current.amount_cents = amount
            current.currency = tx.currency
            current.counterparty = tx.account.name
            current.purpose = tx.display_name or ""
            current.splits[:] = [
                Split(
                    amount_cents=amount,
                    transfer_account=tx.account,
                    source=SplitSource.TRANSFER,
                )
            ]
        # Counter-bookings for transfers that no longer exist.
        for stale in existing.values():
            tx.counterparts.remove(stale)
    session.flush()


def sync_all_counterparts(session: Session, days: int) -> None:
    """Bring counter-bookings of all transfers up to date (idempotent; run at startup).

    Needed for transfers that existed before counter-bookings were introduced and after
    own accounts were added or renamed.
    """
    with_transfers = session.scalars(
        select(Transaction)
        .options(selectinload(Transaction.splits), selectinload(Transaction.counterparts))
        .where(
            Transaction.counterpart_of_id.is_(None),
            Transaction.id.in_(
                select(Split.transaction_id).where(Split.transfer_account_id.is_not(None))
            ),
        )
    )
    sync_counterparts(session, list(with_transfers), days)
