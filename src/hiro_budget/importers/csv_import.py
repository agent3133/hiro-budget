"""CSV exports in the formats of plugins (``*.csv.yaml``, see ``config.CsvFormat``).

A file is read as rows; ``key;value`` lines above the header row (ING: IBAN, account name)
are its metadata. Each data row becomes a booking, except

- rows matching a ``skip`` filter (e.g. pending ones),
- payments funded from a bank account (``funding``): the funding rows and the payment
  they reference form a group whose funding rows become ``ParsedLink``s (the bank booking
  stays the expense) and whose net amount, usually zero, is imported as one booking.

The running balance (``balance`` column) gives the opening and closing balance and the
row order (oldest first or newest first); every row is checked against it.
"""

from __future__ import annotations

import csv
import io
import logging
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from hiro_budget.config import ColumnRef, CsvFilter, CsvFormat, OwnAccount, normalize_iban
from hiro_budget.importers.base import (
    ImportError_,
    ParsedLink,
    ParsedStatement,
    ParsedTransaction,
)

log = logging.getLogger(__name__)

DETECT_LINES = 60  # the header row must be among the first lines


@dataclass
class CsvTable:
    header: list[str]
    rows: list[list[str]]  # data rows, in file order
    metadata: dict[str, str]  # "key;value" lines above the header
    first_row_line: int  # line number of the first data row (for messages)


def _header_index(rows: list[list[str]], wanted: list[str]) -> int | None:
    for i, row in enumerate(rows):
        cells = {c.strip() for c in row}
        if all(w in cells for w in wanted):
            return i
    return None


def read_table(text: str, fmt: CsvFormat) -> CsvTable:
    rows = list(csv.reader(io.StringIO(text), delimiter=fmt.delimiter))
    h = _header_index(rows, fmt.detect.header_has)
    if h is None:
        raise ImportError_(f"header row with {', '.join(fmt.detect.header_has)} not found")
    metadata = {
        row[0].strip(): row[1].strip() for row in rows[:h] if len(row) >= 2 and row[0].strip()
    }
    data = [row for row in rows[h + 1 :] if any(c.strip() for c in row)]
    return CsvTable([c.strip() for c in rows[h]], data, metadata, h + 2)


class _Columns:
    def __init__(self, header: list[str]) -> None:
        self.header = header

    def index(self, ref: ColumnRef) -> int:
        if isinstance(ref, int):
            if not 0 <= ref < len(self.header):
                raise ImportError_(f"column {ref} does not exist ({len(self.header)} columns)")
            return ref
        try:
            return self.header.index(ref)
        except ValueError:
            raise ImportError_(f"column '{ref}' not found") from None


def _cell(row: list[str], i: int | None) -> str:
    return row[i].strip() if i is not None and i < len(row) else ""


def parse_cents(text: str, decimal_comma: bool) -> int:
    v = text.strip().replace(" ", "").replace(" ", "").lstrip("+")
    v = v.replace(".", "").replace(",", ".") if decimal_comma else v.replace(",", "")
    try:
        return int(Decimal(v) * 100)
    except InvalidOperation:
        raise ImportError_(f"not an amount: {text!r}") from None


@dataclass
class _Row:
    line: int
    cells: list[str]
    booking_date: date
    amount_cents: int
    balance_cents: int | None
    id: str


def _matches(f: CsvFilter, cols: _Columns, row: list[str]) -> bool:
    value = _cell(row, cols.index(f.column))
    if f.matches is not None and re.search(f.matches, value):
        return True
    return value in f.values()


def _chain_breaks(rows: list[_Row]) -> int:
    """Rows whose balance does not follow from the one before (rows with a balance)."""
    return sum(
        a.balance_cents is not None and a.balance_cents + b.amount_cents != b.balance_cents
        for a, b in zip(rows, rows[1:], strict=False)
    )


def _chronological(rows: list[_Row], has_balance: bool) -> list[_Row]:
    """Oldest first: the order in which the running balance adds up, else by the dates."""
    if len(rows) < 2:
        return rows
    if has_balance:
        forward, backward = _chain_breaks(rows), _chain_breaks(rows[::-1])
        if forward != backward:
            return rows if forward < backward else rows[::-1]
    return rows[::-1] if rows[0].booking_date > rows[-1].booking_date else rows


class CsvImporter:
    """One configured CSV format; ``own_accounts`` resolve accounts found by e-mail."""

    def __init__(self, fmt: CsvFormat, own_accounts: list[OwnAccount] | None = None) -> None:
        self.fmt = fmt
        self.name = f"csv:{fmt.name}"
        self.own_accounts = own_accounts or []
        self._enricher = None  # set per file (fmt.enrich)

    def detect(self, path: Path) -> bool:
        if path.suffix.lower() != ".csv":
            return False
        try:
            with path.open(encoding=self.fmt.encoding, newline="") as f:
                head = "".join(line for _, line in zip(range(DETECT_LINES), f, strict=False))
        except (OSError, UnicodeDecodeError, LookupError):
            return False
        rows = list(csv.reader(io.StringIO(head), delimiter=self.fmt.delimiter))
        return _header_index(rows, self.fmt.detect.header_has) is not None

    def parse(self, path: Path) -> ParsedStatement:
        try:
            text = path.read_text(encoding=self.fmt.encoding)
        except (OSError, UnicodeDecodeError, LookupError) as e:
            raise ImportError_(f"cannot read {path.name}: {e}") from e
        stmt = self.parse_text(text)
        for w in stmt.warnings:
            log.warning("%s: %s", path.name, w)
        return stmt

    # -- parsing -------------------------------------------------------------------

    def parse_text(self, text: str) -> ParsedStatement:
        fmt, c = self.fmt, self.fmt.columns
        self._enricher = None
        if fmt.enrich:
            from hiro_budget.plugin_api import enricher_for

            self._enricher = enricher_for(bank_code=fmt.enrich, institution=fmt.enrich)
            if self._enricher is None:
                raise ImportError_(f"no plugin enriches '{fmt.enrich}' (enrich in {fmt.name})")
        table = read_table(text, fmt)
        cols = _Columns(table.header)
        i_date, i_amount = cols.index(c.booking_date), cols.index(c.amount)
        i_balance = cols.index(c.balance) if c.balance is not None else None
        i_id = cols.index(c.id) if c.id is not None else None
        i_fee = cols.index(c.fee) if c.fee is not None else None

        rows: list[_Row] = []
        for n, cells in enumerate(table.rows):
            line = table.first_row_line + n
            if not all(_matches(f, cols, cells) for f in fmt.only):
                continue  # e.g. reverted, another currency: not in the balance either
            try:
                fee = (
                    parse_cents(_cell(cells, i_fee) or "0", fmt.decimal_comma)
                    if i_fee is not None
                    else 0
                )
                rows.append(
                    _Row(
                        line,
                        cells,
                        datetime.strptime(_cell(cells, i_date), fmt.date_format).date(),
                        parse_cents(_cell(cells, i_amount), fmt.decimal_comma) - fee,
                        parse_cents(_cell(cells, i_balance), fmt.decimal_comma)
                        if i_balance is not None
                        else None,
                        _cell(cells, i_id),
                    )
                )
            except (ValueError, ImportError_) as e:
                raise ImportError_(f"line {line}: {e}") from None
        if not rows:
            raise ImportError_("no bookings found")
        rows = _chronological(rows, i_balance is not None)

        warnings: list[str] = []
        if i_balance is not None:
            for a, b in zip(rows, rows[1:], strict=False):
                if (
                    a.balance_cents is not None
                    and a.balance_cents + b.amount_cents != b.balance_cents
                ):
                    warnings.append(
                        f"line {b.line}: amount {b.amount_cents / 100:.2f} does not match the "
                        "balance change"
                    )

        bookings, links, skipped = self._split(rows, cols, warnings)
        name, iban, scope = self._account(table, cols, rows)
        stmt = ParsedStatement(
            importer=self.name,
            institution=fmt.institution,
            account_name=name,
            account_kind=fmt.kind,
            account_iban=iban,
            period_start=rows[0].booking_date,
            period_end=rows[-1].booking_date,
            transactions=[self._booking(r, cols, amount) for r, amount in bookings],
            links=links,
            warnings=warnings,
            dedupe_scope=scope,
            match_by_date_amount=fmt.match_by == "date_amount",
        )
        if i_balance is not None:
            first, last = rows[0], rows[-1]
            assert first.balance_cents is not None and last.balance_cents is not None
            stmt.opening_balance_cents = first.balance_cents - first.amount_cents
            # the skipped rows (pending) are not in the bookings yet
            stmt.closing_balance_cents = last.balance_cents - sum(r.amount_cents for r in skipped)
        return stmt

    def _split(
        self, rows: list[_Row], cols: _Columns, warnings: list[str]
    ) -> tuple[list[tuple[_Row, int]], list[ParsedLink], list[_Row]]:
        """Bookings (row, amount to import), links and skipped rows."""
        fmt = self.fmt
        funding = fmt.funding
        is_funding = (
            {id(r) for r in rows if _matches(funding.rows, cols, r.cells)} if funding else set()
        )
        skipped = [
            r
            for r in rows
            if id(r) not in is_funding and any(_matches(f, cols, r.cells) for f in fmt.skip)
        ]
        skipped_ids = {id(r) for r in skipped}
        if not funding or not is_funding:
            return [(r, r.amount_cents) for r in rows if id(r) not in skipped_ids], [], skipped

        # groups: a funded payment and every row naming it in the linked_by column
        i_link = cols.index(funding.linked_by)
        by_id = {r.id: r for r in rows if r.id}
        members: dict[str, list[_Row]] = defaultdict(list)
        for r in rows:
            if ref := _cell(r.cells, i_link):
                members[ref].append(r)
        groups = {ref: ms for ref, ms in members.items() if any(id(m) in is_funding for m in ms)}

        bookings: list[tuple[_Row, int]] = []
        links: list[ParsedLink] = []
        grouped: set[int] = set()
        for ref, ms in groups.items():
            payment = by_id.get(ref)
            group = [payment, *ms] if payment is not None else ms
            grouped.update(id(r) for r in group)
            if payment is not None and id(payment) in skipped_ids:
                skipped.extend(r for r in ms if id(r) not in skipped_ids)
                continue
            source = payment or ms[0]
            payee = _cell(source.cells, self._opt(cols, fmt.columns.counterparty))
            description = self._purpose(source.cells, cols)
            if payment is None:
                warnings.append(f"line {ms[0].line}: the payment {ref} it funds is not in the file")
            for f in (m for m in ms if id(m) in is_funding):
                links.append(
                    ParsedLink(
                        external_id=f.id or f"{ref}:{f.line}",
                        booking_date=f.booking_date,
                        amount_cents=-f.amount_cents,
                        payee=payee,
                        description=description,
                        bank_match=funding.bank_match,
                    )
                )
            rest = sum(r.amount_cents for r in group)
            if rest:  # partly from the PayPal balance, or a refund kept there
                bookings.append((source, rest))
        bookings += [
            (r, r.amount_cents) for r in rows if id(r) not in grouped and id(r) not in skipped_ids
        ]
        order = {id(r): n for n, r in enumerate(rows)}
        bookings.sort(key=lambda b: order[id(b[0])])
        return bookings, links, skipped

    @staticmethod
    def _opt(cols: _Columns, ref: ColumnRef | None) -> int | None:
        return cols.index(ref) if ref is not None else None

    def _purpose(self, cells: list[str], cols: _Columns) -> str:
        parts = (_cell(cells, cols.index(ref)) for ref in self.fmt.columns.purpose)
        return " ".join(p for p in parts if p)

    def _value(self, field: str, value: str) -> str:
        return self.fmt.values.get(field, {}).get(value, value)

    def _booking(self, r: _Row, cols: _Columns, amount: int) -> ParsedTransaction:
        c = self.fmt.columns
        counterparty = self._value("counterparty", _cell(r.cells, self._opt(cols, c.counterparty)))
        booking_type = self._value("booking_type", _cell(r.cells, self._opt(cols, c.booking_type)))
        purpose = self._purpose(r.cells, cols)
        value_date = None
        if raw := _cell(r.cells, self._opt(cols, c.value_date)):
            try:
                value_date = datetime.strptime(raw, self.fmt.date_format).date()
            except ValueError:
                value_date = None
        merchant = _cell(r.cells, self._opt(cols, c.merchant)) or None
        purchase_date = None
        if self._enricher is not None:
            merchant, purchase_date = self._enricher(counterparty, purpose, r.booking_date)
        return ParsedTransaction(
            booking_date=r.booking_date,
            value_date=value_date,
            amount_cents=amount,
            booking_type=booking_type or self.fmt.name,
            counterparty=counterparty,
            purpose=purpose,
            reference=_cell(r.cells, self._opt(cols, c.reference)) or None,
            merchant=merchant,
            card_purchase_date=purchase_date,
            raw_text=self.fmt.delimiter.join(r.cells),
            external_id=r.id or None,
        )

    def _account(
        self, table: CsvTable, cols: _Columns, rows: list[_Row]
    ) -> tuple[str, str | None, str | None]:
        """Account name, IBAN and dedupe scope."""
        a, fmt = self.fmt.account, self.fmt
        if a.email_column:
            i = cols.index(a.email_column)
            senders = Counter(_cell(r.cells, i).lower() for r in rows if r.amount_cents < 0)
            senders.pop("", None)
            if not senders:
                raise ImportError_(f"no e-mail address in column '{a.email_column}'")
            email = senders.most_common(1)[0][0]
            account = next((o for o in self.own_accounts if (o.email or "").lower() == email), None)
            if account is None:
                raise ImportError_(
                    f"no own account with e-mail {email}: add one with kind: {fmt.kind} and "
                    f"email: {email} to own_accounts in config.yaml"
                )
            return account.name, None, f"email:{email}"
        iban = None
        if a.iban_from:
            raw = table.metadata.get(a.iban_from)
            if not raw:
                raise ImportError_(f"no '{a.iban_from}' line above the header")
            iban = normalize_iban(raw)
        if a.name:
            return a.name, iban, None
        if a.name_from and (product := table.metadata.get(a.name_from)):
            return f"{fmt.institution} {product}".strip(), iban, None
        return fmt.name, iban, None
