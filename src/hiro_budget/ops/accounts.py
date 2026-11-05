"""Operation: accounts and balances."""

from __future__ import annotations

from pydantic import Field

from hiro_budget.ops import Args, OpContext, Operation, Result
from hiro_budget.ops.text import Column, dot, dot_or_none, eur, gap_text, table


class ListAccountsArgs(Args):
    pass


class AccountInfo(Result):
    id: int
    name: str
    kind: str
    institution: str | None
    iban: str | None
    balance: str = Field(description="euros, e.g. '1234.56'")
    last_booking: str | None
    bookings: int
    statement_balance: str | None
    statement_date: str | None
    gaps: list[str]


class ListAccountsResult(Result):
    accounts: list[AccountInfo]
    total: str


def run(ctx: OpContext, a: ListAccountsArgs) -> ListAccountsResult:
    from hiro_budget.services.accounts import account_balances

    with ctx.session() as s:
        balances = account_balances(s)
    infos = [
        AccountInfo(
            id=b.account_id,
            name=b.name,
            kind=str(b.kind),
            institution=b.institution,
            iban=b.iban,
            balance=dot(b.balance_cents),
            last_booking=b.last_booking.isoformat() if b.last_booking else None,
            bookings=b.booking_count,
            statement_balance=dot_or_none(b.statement_balance_cents),
            statement_date=b.statement_date.isoformat() if b.statement_date else None,
            gaps=[gap_text(b.name, g) for g in b.gaps],
        )
        for b in balances
    ]
    return ListAccountsResult(accounts=infos, total=dot(sum(b.balance_cents for b in balances)))


def render(r: ListAccountsResult, width: int | None = None) -> str:
    cols = [
        Column("Account", shrink=True),
        Column("Kind"),
        Column("Balance", "r"),
        Column("Last booking"),
        Column("Bookings", "r"),
    ]
    rows = [
        [a.name, a.kind, eur(a.balance), a.last_booking or "", str(a.bookings)] for a in r.accounts
    ]
    out = [table(cols, rows, width), f"Total: {eur(r.total)}"]
    out += [f"Gap: {g}" for a in r.accounts for g in a.gaps]
    return "\n".join(out)


OPERATIONS = [
    Operation(
        name="list_accounts",
        cli=("accounts", "list"),
        summary="List accounts with balances",
        description=(
            "All accounts with their current balance (euros as strings with a dot), kind, "
            "last booking, closing balance of the latest statement and missing-statement "
            "gaps. Use the account name or IBAN wherever an account is asked for."
        ),
        args=ListAccountsArgs,
        result=ListAccountsResult,
        run=run,
        render=render,
        read_only=True,
    )
]
