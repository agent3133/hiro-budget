"""Operations: list and show bookings."""

from __future__ import annotations

from datetime import date

from pydantic import Field

from hiro_budget.ops import Args, OpContext, Operation, Result, flag, positional, resolve
from hiro_budget.ops.text import Column, day, dot, dot_or_none, eur, page_note, table


class SplitInfo(Result):
    category: str | None = Field(description="category key 'group.category'; None: none")
    category_name: str | None
    transfer_account: str | None
    amount: str
    memo: str | None
    source: str = Field(description="manual, rule, ml (a suggestion), mortgage, transfer, ...")
    rule_id: str | None
    confidence: float | None
    needs_category: bool


class BookingInfo(Result):
    id: int
    date: str
    account: str
    payee: str
    purpose: str
    amount: str
    category: str = Field(description="category name, 'split', 'transfer: <account>' or ''")
    uncategorized: bool
    needs_review: bool = Field(description="the category is a suggestion of the classifier")
    manual: bool = Field(description="entered by hand, not imported")


class ListBookingsArgs(Args):
    date_from: date | None = Field(
        None, description="first booking date (YYYY-MM-DD)", json_schema_extra=flag("--from")
    )
    date_to: date | None = Field(
        None, description="last booking date (YYYY-MM-DD)", json_schema_extra=flag("--to")
    )
    account: str | None = Field(None, description="account name or IBAN")
    category: str | None = Field(
        None,
        description=(
            "category key or name; or 'uncategorized', 'review' (suggestions to confirm), "
            "'transfers', or 'group:<key>' for all categories of a group"
        ),
    )
    text: str | None = Field(
        None, description="search in payee, purpose, notes (an amount also matches its amount)"
    )
    amount: str | None = Field(
        None,
        description=(
            "amount query: '17,99' (+-17,99), '-17,99' (only that expense), '17' (17,00-17,99), "
            "'100..200', '>500', '<=50'"
        ),
    )
    limit: int = Field(50, ge=1, le=500, description="how many bookings (newest first)")
    offset: int = Field(0, ge=0, description="skip this many bookings")


class ListBookingsResult(Result):
    bookings: list[BookingInfo]
    total: int = Field(description="all bookings matching the filter")
    offset: int


def _category_label(row) -> str:
    if len(row.splits) > 1:
        return "split"
    if not row.splits:
        return ""
    sp = row.splits[0]
    if sp.category_name:
        return sp.category_name
    if sp.transfer_account_name:
        return f"transfer: {sp.transfer_account_name}"
    return ""


def booking_info(row) -> BookingInfo:
    return BookingInfo(
        id=row.id,
        date=row.booking_date.isoformat(),
        account=row.account_name,
        payee=row.payee,
        purpose=row.purpose,
        amount=dot(row.amount_cents),
        category=_category_label(row),
        uncategorized=row.is_uncategorized,
        needs_review=row.needs_review,
        manual=row.is_manual_entry,
    )


def run_list(ctx: OpContext, a: ListBookingsArgs) -> ListBookingsResult:
    from hiro_budget.services import transactions as txs

    with ctx.session() as s:
        f = txs.TxFilter(
            date_from=a.date_from,
            date_to=a.date_to,
            account_id=resolve.account(s, a.account).id if a.account else None,
            category=resolve.category_filter(s, a.category) if a.category else txs.ALL,
            text=a.text or "",
            amount=a.amount or "",
        )
        total = txs.count_transactions(s, f)
        rows = txs.list_transactions(s, f, limit=a.limit, offset=a.offset)
    return ListBookingsResult(
        bookings=[booking_info(r) for r in rows], total=total, offset=a.offset
    )


def render_list(r: ListBookingsResult, width: int | None = None) -> str:
    cols = [
        Column("Id", "r"),
        Column("Date"),
        Column("Account", shrink=True),
        Column("Payee", shrink=True),
        Column("Amount", "r"),
        Column("Category", shrink=True),
    ]
    if not r.bookings:
        return page_note(0, r.total, r.offset)
    rows = []
    for b in r.bookings:
        mark = "?" if b.uncategorized else ("~" if b.needs_review else "")
        rows.append(
            [
                str(b.id),
                day(date.fromisoformat(b.date)),
                b.account,
                b.payee,
                eur(b.amount),
                f"{mark}{b.category}" if mark != "?" else "(uncategorized)",
            ]
        )
    note = page_note(len(r.bookings), r.total, r.offset)
    legend = (
        "~ = suggestion of the classifier, not confirmed"
        if any(b.needs_review for b in r.bookings)
        else ""
    )
    return "\n".join(x for x in (table(cols, rows, width), note, legend) if x)


class ShowBookingArgs(Args):
    id: int = Field(description="booking id (from list_bookings)", json_schema_extra=positional())


class PayeeBookingInfo(Result):
    id: int
    date: str
    amount: str


class PayeeHistoryInfo(Result):
    name: str
    bookings_total: int
    bookings_last_12_months: int
    average_last_12_months: str
    previous_amount: str | None = Field(description="the steady amount before this one changed")
    last_bookings: list[PayeeBookingInfo]


class ShowBookingResult(Result):
    id: int
    date: str
    value_date: str | None
    account: str
    booking_type: str
    payee: str
    counterparty: str
    merchant: str | None
    purpose: str
    amount: str
    notes: str | None
    manual: bool
    counterpart_of: str | None = Field(description="account of the booking this one mirrors")
    counterpart_of_id: int | None
    counterparts: list[str] = Field(description="'id account' of mirror bookings")
    mandate_ref: str | None
    reference: str | None
    details: str | None
    splits: list[SplitInfo]
    payee_history: PayeeHistoryInfo | None


def run_show(ctx: OpContext, a: ShowBookingArgs) -> ShowBookingResult:
    from hiro_budget.services import transactions as txs

    with ctx.session() as s:
        row = txs.get_row(s, a.id)
        hist = txs.payee_history(s, a.id)
    history = None
    if hist is not None:
        history = PayeeHistoryInfo(
            name=hist.name,
            bookings_total=len(hist.ids),
            bookings_last_12_months=hist.year_count,
            average_last_12_months=dot(hist.year_average),
            previous_amount=dot_or_none(hist.previous_amount),
            last_bookings=[
                PayeeBookingInfo(
                    id=b.id, date=b.booking_date.isoformat(), amount=dot(b.amount_cents)
                )
                for b in hist.last
            ],
        )
    return ShowBookingResult(
        id=row.id,
        date=row.booking_date.isoformat(),
        value_date=row.value_date.isoformat() if row.value_date else None,
        account=row.account_name,
        booking_type=row.booking_type,
        payee=row.payee,
        counterparty=row.counterparty,
        merchant=row.merchant,
        purpose=row.purpose,
        amount=dot(row.amount_cents),
        notes=row.notes,
        manual=row.is_manual_entry,
        counterpart_of=row.counterpart_of,
        counterpart_of_id=row.counterpart_of_id,
        counterparts=[f"{i} {name}" for i, name in row.counterparts],
        mandate_ref=row.mandate_ref,
        reference=row.reference,
        details=row.details,
        splits=[
            SplitInfo(
                category=sp.category_key,
                category_name=sp.category_name,
                transfer_account=sp.transfer_account_name,
                amount=dot(sp.amount_cents),
                memo=sp.memo,
                source=str(sp.source),
                rule_id=sp.rule_id,
                confidence=sp.confidence,
                needs_category=sp.needs_category,
            )
            for sp in row.splits
        ],
        payee_history=history,
    )


def render_show(r: ShowBookingResult, width: int | None = None) -> str:
    lines = [
        f"Booking {r.id}  {day(date.fromisoformat(r.date))}  {r.account}",
        f"Payee:    {r.payee}",
        f"Amount:   {eur(r.amount)}",
        f"Type:     {r.booking_type}",
        f"Purpose:  {r.purpose}",
    ]
    if r.notes:
        lines.append(f"Notes:    {r.notes}")
    if r.manual:
        lines.append("Entered by hand")
    if r.counterpart_of:
        lines.append(f"Mirror of booking {r.counterpart_of_id} ({r.counterpart_of})")
    if r.counterparts:
        lines.append(f"Mirrored by: {', '.join(r.counterparts)}")
    lines.append("Splits:")
    for sp in r.splits:
        what = sp.category or (
            f"transfer: {sp.transfer_account}" if sp.transfer_account else "(uncategorized)"
        )
        extra = [sp.source]
        if sp.rule_id:
            extra.append(f"rule {sp.rule_id}")
        if sp.confidence is not None:
            extra.append(f"confidence {sp.confidence:.2f}")
        if sp.memo:
            extra.append(sp.memo)
        lines.append(f"  {eur(sp.amount):>12}  {what}  [{', '.join(extra)}]")
    h = r.payee_history
    if h is not None:
        lines.append(
            f"Payee '{h.name}': {h.bookings_total} bookings, {h.bookings_last_12_months} in the "
            f"last 12 months (average {eur(h.average_last_12_months)})"
        )
        if h.previous_amount is not None:
            lines.append(f"  amount changed, it was {eur(h.previous_amount)}")
        lines += [
            f"  {b.id}  {day(date.fromisoformat(b.date))}  {eur(b.amount)}" for b in h.last_bookings
        ]
    return "\n".join(lines)


OPERATIONS = [
    Operation(
        name="list_bookings",
        cli=("tx", "list"),
        summary="List bookings with filters (newest first)",
        description=(
            "Bookings, newest first, with the filters of the bookings page. Every booking "
            "has an id for the other tools. Use category 'uncategorized' to find bookings "
            "that still need a category and 'review' for suggestions to confirm. Paged: "
            "'total' is the number of all matches, use offset for the next page. Amounts "
            "are euros as strings with a dot, negative for expenses."
        ),
        args=ListBookingsArgs,
        result=ListBookingsResult,
        run=run_list,
        render=render_list,
        read_only=True,
    ),
    Operation(
        name="get_booking",
        cli=("tx", "show"),
        summary="Show one booking with its splits and payee history",
        description=(
            "One booking in full: all fields, its splits (category or transfer, amount, "
            "where the category came from: manual, rule, ml suggestion), mirror bookings of "
            "transfers and the earlier bookings of the same payee."
        ),
        args=ShowBookingArgs,
        result=ShowBookingResult,
        run=run_show,
        render=render_show,
        read_only=True,
    ),
]
