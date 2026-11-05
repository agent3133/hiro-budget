"""Operation: import history (statements per account)."""

from __future__ import annotations

from pydantic import Field

from hiro_budget.ops import Args, OpContext, Operation, Result, resolve
from hiro_budget.ops.text import Column, eur, gap_text, table


class ListImportsArgs(Args):
    account: str | None = Field(None, description="only this account (name or IBAN)")
    limit: int = Field(50, ge=1, le=500, description="how many imports (newest first)")


class ImportInfo(Result):
    id: int
    account: str
    file: str
    importer: str
    imported_at: str
    period_start: str | None
    period_end: str | None
    opening_balance: str | None
    closing_balance: str | None
    bookings: int


class ListImportsResult(Result):
    imports: list[ImportInfo]
    gaps: list[str]


def run(ctx: OpContext, a: ListImportsArgs) -> ListImportsResult:
    from sqlalchemy import func, select

    from hiro_budget.db.models import Account, ImportBatch, Transaction
    from hiro_budget.ops.text import dot_or_none
    from hiro_budget.services.accounts import account_balances

    with ctx.session() as s:
        q = (
            select(
                ImportBatch,
                Account.name,
                func.count(Transaction.id),
            )
            .join(Account, Account.id == ImportBatch.account_id)
            .outerjoin(Transaction, Transaction.batch_id == ImportBatch.id)
            .group_by(ImportBatch.id)
            .order_by(ImportBatch.imported_at.desc(), ImportBatch.id.desc())
            .limit(a.limit)
        )
        account_name = None
        if a.account:
            acc = resolve.account(s, a.account)
            account_name = acc.name
            q = q.where(ImportBatch.account_id == acc.id)
        infos = [
            ImportInfo(
                id=b.id,
                account=name,
                file=b.source_file,
                importer=b.importer,
                imported_at=f"{b.imported_at:%Y-%m-%d %H:%M}",
                period_start=b.period_start.isoformat() if b.period_start else None,
                period_end=b.period_end.isoformat() if b.period_end else None,
                opening_balance=dot_or_none(b.opening_balance_cents),
                closing_balance=dot_or_none(b.closing_balance_cents),
                bookings=count,
            )
            for b, name, count in s.execute(q)
        ]
        gaps = [
            gap_text(b.name, g)
            for b in account_balances(s)
            if account_name in (None, b.name)
            for g in b.gaps
        ]
    return ListImportsResult(imports=infos, gaps=gaps)


def render(r: ListImportsResult, width: int | None = None) -> str:
    cols = [
        Column("Id", "r"),
        Column("Account", shrink=True),
        Column("Period"),
        Column("Closing", "r"),
        Column("Bookings", "r"),
        Column("File", shrink=True),
    ]
    rows = [
        [
            str(i.id),
            i.account,
            f"{i.period_start or '?'} - {i.period_end or '?'}",
            eur(i.closing_balance),
            str(i.bookings),
            i.file,
        ]
        for i in r.imports
    ]
    out = [table(cols, rows, width)]
    out += [f"Gap: {g}" for g in r.gaps]
    return "\n".join(out)


OPERATIONS = [
    Operation(
        name="list_imports",
        cli=("imports", "list"),
        summary="List imported statements and statement gaps",
        description=(
            "Imported statements (newest first) with account, period, balances and number "
            "of bookings, plus gaps where statements are missing."
        ),
        args=ListImportsArgs,
        result=ListImportsResult,
        run=run,
        render=render,
        read_only=True,
    )
]
