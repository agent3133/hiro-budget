"""Operations that change bookings: categorize, split, transfer, notes, manual bookings."""

from __future__ import annotations

from datetime import date

from pydantic import Field

from hiro_budget.ops import (
    Args,
    Changed,
    OpContext,
    Operation,
    OpError,
    flag,
    positional,
    render_changed,
    resolve,
)
from hiro_budget.ops.bookings import _category_label
from hiro_budget.ops.text import dot, money

IDS = "booking ids (from list_bookings)"


def _labels(s, ids: list[int]) -> dict[int, str]:
    """The current category of each booking; unknown ids are an error."""
    from hiro_budget.services import transactions as txs

    unique_ids = tuple(dict.fromkeys(ids))
    rows = {r.id: r for r in txs.list_transactions(s, txs.TxFilter(ids=unique_ids))}
    missing = [i for i in unique_ids if i not in rows]
    if missing:
        raise OpError(f"no booking {', '.join(str(i) for i in missing)}")
    return {i: _category_label(rows[i]) or "(uncategorized)" for i in unique_ids}


# --- categorize ---------------------------------------------------------------------


class CategorizeArgs(Args):
    ids: list[int] = Field(description=IDS, min_length=1, json_schema_extra=positional())
    category: str = Field(
        description="category key or exact name (see list_categories)",
        json_schema_extra=positional(),
    )


def run_categorize(ctx: OpContext, a: CategorizeArgs) -> Changed:
    from hiro_budget.services import transactions as txs

    with ctx.write("categorize_bookings", a), ctx.session() as s:
        cat = resolve.category(s, a.category)
        before = _labels(s, a.ids)
        transfers = _transfers_of(s, before)
        for i in dict.fromkeys(a.ids):
            txs.set_category(s, i, cat.id, ctx.settings.app.matching.days)
        after = _labels(s, a.ids)
        warnings = _unpaired(s, transfers)
    return Changed(
        message=f"{len(before)} bookings set to {cat.key}",
        details=[f"{i}: {before[i]} -> {after[i]}" for i in before],
        ids=list(before),
        warnings=warnings,
    )


def _transfers_of(s, ids) -> dict[int, tuple[int, int, date, set[int]]]:
    """Per booking that is a transfer now: (account, amount, date, accounts it transfers to)."""
    from sqlalchemy import select

    from hiro_budget.db.models import Split, Transaction

    out: dict[int, tuple[int, int, date, set[int]]] = {}
    q = (
        select(Transaction, Split.transfer_account_id)
        .join(Split, Split.transaction_id == Transaction.id)
        .where(Transaction.id.in_(tuple(ids)), Split.transfer_account_id.is_not(None))
    )
    for tx, target in s.execute(q):
        out.setdefault(tx.id, (tx.account_id, tx.amount_cents, tx.booking_date, set()))[3].add(
            target
        )
    return out


def _unpaired(s, transfers: dict[int, tuple[int, int, date, set[int]]]) -> list[str]:
    """Bookings that stopped being a transfer although the other account still has its partner
    (an imported booking of the opposite amount that is a transfer back): that partner is now
    an unpaired transfer (#93). The GUI does the same; here the caller is told."""
    from sqlalchemy import select

    from hiro_budget.db.models import Account, Split, Transaction

    now = _transfers_of(s, transfers)
    warnings = []
    for tx_id, (account_id, cents, day, targets) in transfers.items():
        gone = targets - now.get(tx_id, (0, 0, day, set()))[3]
        if not gone:
            continue
        q = (
            select(Transaction)
            .join(Split, Split.transaction_id == Transaction.id)
            .where(
                Transaction.account_id.in_(gone),
                Transaction.amount_cents == -cents,
                Transaction.id != tx_id,
                Transaction.counterpart_of_id.is_(None),
                Split.transfer_account_id == account_id,
            )
        )
        partners = list(s.scalars(q))
        if not partners:
            continue
        partner = min(partners, key=lambda p: abs((p.booking_date - day).days))
        mine = s.get_one(Account, account_id).name
        warnings.append(
            f"booking {tx_id} is no longer a transfer; its partner {partner.id} "
            f"({partner.account.name}, {money(partner.amount_cents)}) is still a transfer "
            f"to {mine} and now unpaired. Reset {tx_id} to pair them again."
        )
    return warnings


# --- split --------------------------------------------------------------------------


class SplitArgs(Args):
    id: int = Field(description="booking id", json_schema_extra=positional())
    part: list[str] = Field(
        description=(
            "one per split: CATEGORY=AMOUNT, or transfer:ACCOUNT=AMOUNT; the amounts must "
            "add up to the booking (with its sign), e.g. lebensmittel.supermarkt=-40,00"
        ),
        min_length=1,
    )


def run_split(ctx: OpContext, a: SplitArgs) -> Changed:
    from hiro_budget.services import transactions as txs

    with ctx.write("split_booking", a), ctx.session() as s:
        parts = []
        for text in a.part:
            what, sep, amount = text.rpartition("=")
            if not sep or not what:
                raise OpError(f"'{text}' is not CATEGORY=AMOUNT")
            if what.startswith("transfer:"):
                acc = resolve.account(s, what.removeprefix("transfer:"))
                parts.append(txs.SplitInput(resolve.cents(amount), transfer_account_id=acc.id))
            else:
                parts.append(
                    txs.SplitInput(resolve.cents(amount), category_id=resolve.category(s, what).id)
                )
        txs.set_splits(s, a.id, parts, ctx.settings.app.matching.days)
        after = txs.get_row(s, a.id)
    return Changed(
        message=f"booking {a.id} split into {len(parts)} parts",
        details=[
            f"{money(sp.amount_cents)}  {sp.category_key or sp.transfer_account_name}"
            for sp in after.splits
        ],
        ids=[a.id],
    )


# --- transfer, accept, reset, note ---------------------------------------------------


class TransferArgs(Args):
    id: int = Field(description="booking id", json_schema_extra=positional())
    account: str = Field(
        description="the other own account (name or IBAN)", json_schema_extra=positional()
    )


def run_transfer(ctx: OpContext, a: TransferArgs) -> Changed:
    from hiro_budget.services import transactions as txs

    with ctx.write("mark_transfer", a), ctx.session() as s:
        acc = resolve.account(s, a.account)
        before = _labels(s, [a.id])
        txs.mark_transfer(s, a.id, acc.id, ctx.settings.app.matching.days)
    return Changed(
        message=f"booking {a.id} is a transfer to {acc.name}",
        details=[f"{a.id}: {before[a.id]} -> transfer: {acc.name}"],
        ids=[a.id],
    )


class IdsArgs(Args):
    ids: list[int] = Field(description=IDS, min_length=1, json_schema_extra=positional())


def run_accept(ctx: OpContext, a: IdsArgs) -> Changed:
    from hiro_budget.services import transactions as txs

    with ctx.write("accept_suggestions", a), ctx.session() as s:
        before = _labels(s, a.ids)
        rows = txs.list_transactions(s, txs.TxFilter(ids=tuple(before)))
        none = [str(r.id) for r in rows if not r.needs_review]
        if none:  # all or none; the GUI only offers the bookings with a suggestion
            raise OpError(
                f"no suggestion to accept on booking {', '.join(none)} "
                "(list_bookings with category 'review' shows them)"
            )
        for i in dict.fromkeys(a.ids):
            txs.accept_suggestion(s, i)
    return Changed(
        message=f"{len(before)} suggestions accepted",
        details=[f"{i}: {label}" for i, label in before.items()],
        ids=list(before),
    )


def run_reset(ctx: OpContext, a: IdsArgs) -> Changed:
    from hiro_budget.services import transactions as txs

    with ctx.write("reset_bookings", a), ctx.session() as s:
        before = _labels(s, a.ids)
        txs.reset_to_automatic(s, ctx.settings, list(dict.fromkeys(a.ids)))
        after = _labels(s, a.ids)
    return Changed(
        message=f"{len(before)} bookings categorized automatically again",
        details=[f"{i}: {before[i]} -> {after[i]}" for i in before],
        ids=list(before),
    )


class NoteArgs(Args):
    id: int = Field(description="booking id", json_schema_extra=positional())
    text: str = Field(description="the note; empty removes it", json_schema_extra=positional())


def run_note(ctx: OpContext, a: NoteArgs) -> Changed:
    from hiro_budget.services import transactions as txs

    with ctx.write("set_booking_note", a), ctx.session() as s:
        _labels(s, [a.id])
        txs.set_notes(s, a.id, a.text)
    return Changed(
        message=f"note of booking {a.id} " + ("set" if a.text.strip() else "removed"), ids=[a.id]
    )


# --- manual bookings -----------------------------------------------------------------


class AddArgs(Args):
    account: str = Field(description="account name or IBAN")
    booking_date: date = Field(description="YYYY-MM-DD", json_schema_extra=flag("--date"))
    amount: str = Field(description="euros, negative for an expense, e.g. -12.50")
    payee: str = Field(description="who was paid / who paid")
    purpose: str = Field("", description="purpose text")
    category: str | None = Field(None, description="category key or name (default: automatic)")
    transfer: str | None = Field(None, description="the other own account, for a transfer")
    notes: str | None = Field(None, description="a note")


def _entry(s, a: AddArgs):
    from hiro_budget.services.transactions import ManualEntry

    return ManualEntry(
        account_id=resolve.account(s, a.account).id,
        booking_date=a.booking_date,
        amount_cents=resolve.cents(a.amount),
        payee=a.payee,
        purpose=a.purpose,
        category_id=resolve.category(s, a.category).id if a.category else None,
        transfer_account_id=resolve.account(s, a.transfer).id if a.transfer else None,
        notes=a.notes,
    )


def run_add(ctx: OpContext, a: AddArgs) -> Changed:
    from hiro_budget.services import transactions as txs

    with ctx.write("create_manual_booking", a), ctx.session() as s:
        new_id = txs.create_manual(s, ctx.settings, _entry(s, a))
        label = _labels(s, [new_id])[new_id]
    return Changed(
        message=f"booking {new_id} created", details=[f"{new_id}: {label}"], ids=[new_id]
    )


class EditArgs(Args):
    id: int = Field(description="id of a manual booking", json_schema_extra=positional())
    account: str | None = Field(None, description="account name or IBAN")
    booking_date: date | None = Field(
        None, description="YYYY-MM-DD", json_schema_extra=flag("--date")
    )
    amount: str | None = Field(None, description="euros, negative for an expense")
    payee: str | None = Field(None, description="payee")
    purpose: str | None = Field(None, description="purpose text")
    category: str | None = Field(None, description="category key or name")
    transfer: str | None = Field(None, description="the other own account, for a transfer")
    notes: str | None = Field(None, description="a note")


def run_edit(ctx: OpContext, a: EditArgs) -> Changed:
    from dataclasses import replace

    from hiro_budget.services import transactions as txs

    with ctx.write("update_manual_booking", a), ctx.session() as s:
        entry = txs.get_manual(s, a.id)
        changes: dict = {}
        if a.account:
            changes["account_id"] = resolve.account(s, a.account).id
        if a.booking_date:
            changes["booking_date"] = a.booking_date
        if a.amount:
            changes["amount_cents"] = resolve.cents(a.amount)
        if a.payee is not None:
            changes["payee"] = a.payee
        if a.purpose is not None:
            changes["purpose"] = a.purpose
        if a.category:
            changes["category_id"] = resolve.category(s, a.category).id
            changes["transfer_account_id"] = None
        if a.transfer:
            changes["transfer_account_id"] = resolve.account(s, a.transfer).id
            changes["category_id"] = None
        if a.notes is not None:
            changes["notes"] = a.notes
        if not changes:
            raise OpError("nothing to change; give at least one option")
        txs.update_manual(s, ctx.settings, a.id, replace(entry, **changes))
        label = _labels(s, [a.id])[a.id]
    return Changed(message=f"booking {a.id} changed", details=[f"{a.id}: {label}"], ids=[a.id])


class DeleteArgs(Args):
    id: int = Field(description="id of a manual booking", json_schema_extra=positional())


def run_delete(ctx: OpContext, a: DeleteArgs) -> Changed:
    from hiro_budget.services import transactions as txs

    with ctx.write("delete_manual_booking", a), ctx.session() as s:
        row = txs.get_row(s, a.id)
        txs.delete_manual(s, a.id)
    return Changed(
        message=f"booking {a.id} deleted",
        details=[f"{row.booking_date} {row.payee} {dot(row.amount_cents)}"],
        ids=[a.id],
    )


def _op(name, cli, summary, description, args, run) -> Operation:
    return Operation(
        name=name,
        cli=cli,
        summary=summary,
        description=description,
        args=args,
        result=Changed,
        run=run,
        render=render_changed,
        read_only=False,
    )


OPERATIONS = [
    _op(
        "categorize_bookings",
        ("tx", "categorize"),
        "Set the category of bookings",
        "Give one or more bookings (ids from list_bookings) a category. Counter-bookings of "
        "transfers cannot be changed; change the original booking. All or none are changed.",
        CategorizeArgs,
        run_categorize,
    ),
    _op(
        "split_booking",
        ("tx", "split"),
        "Split a booking over categories",
        "Replace the splits of a booking. Each part is CATEGORY=AMOUNT (or "
        "transfer:ACCOUNT=AMOUNT); the amounts must add up to the booking amount with its "
        "sign (an expense is negative).",
        SplitArgs,
        run_split,
    ),
    _op(
        "mark_transfer",
        ("tx", "transfer"),
        "Mark a booking as a transfer to another own account",
        "Turn a booking into a transfer between own accounts. The counter-booking in the "
        "other account is kept in sync.",
        TransferArgs,
        run_transfer,
    ),
    _op(
        "accept_suggestions",
        ("tx", "accept"),
        "Confirm the classifier's suggestions",
        "Turn suggested categories (source ml, shown as review) into confirmed ones.",
        IdsArgs,
        run_accept,
    ),
    _op(
        "reset_bookings",
        ("tx", "reset"),
        "Categorize bookings automatically again",
        "Drop the manual categories of the bookings and run rules and classifier again.",
        IdsArgs,
        run_reset,
    ),
    _op(
        "set_booking_note",
        ("tx", "note"),
        "Set the note of a booking",
        "Set or (with empty text) remove the note of a booking.",
        NoteArgs,
        run_note,
    ),
    _op(
        "create_manual_booking",
        ("tx", "add"),
        "Add a booking by hand",
        "Create a booking that was not imported (e.g. cash). Amount in euros, negative for "
        "an expense. Without a category it is categorized automatically.",
        AddArgs,
        run_add,
    ),
    _op(
        "update_manual_booking",
        ("tx", "edit"),
        "Change a booking entered by hand",
        "Change fields of a booking created by hand; fields left out stay as they are. "
        "Imported bookings can only be categorized.",
        EditArgs,
        run_edit,
    ),
    _op(
        "delete_manual_booking",
        ("tx", "delete"),
        "Delete a booking entered by hand",
        "Delete a booking created by hand (and the counter-bookings of its transfer). "
        "Imported bookings cannot be deleted.",
        DeleteArgs,
        run_delete,
    ),
]
