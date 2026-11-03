"""Common data structures of everything that brings bookings into the app (plugin API 2).

A file importer turns a source file (PDF, CSV, ...) into a ``ParsedStatement``; a connector
(``importers.connectors``) fetches one from a bank. Neither knows anything about the
database: ``services.import_service`` persists both. They come from plugins
(``importers.plugins``); plugin code uses these types through ``plugin_api``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Protocol


@dataclass
class ParsedTransaction:
    booking_date: date
    amount_cents: int
    booking_type: str
    counterparty: str = ""
    purpose: str = ""
    value_date: date | None = None
    mandate_ref: str | None = None
    reference: str | None = None
    merchant: str | None = None
    card_purchase_date: date | None = None
    raw_text: str = ""
    external_id: str | None = None  # the source's own transaction ID (CSV): dedupe key


@dataclass
class ParsedLink:
    """A payment that a bank booking paid for (PayPal funded from the Giro): not imported
    as a booking, the bank booking gets its payee and article (``db.models.LinkedPayment``)."""

    external_id: str
    booking_date: date
    amount_cents: int  # as the bank booking shows it
    payee: str = ""
    description: str = ""
    bank_match: str = ""


@dataclass
class ParsedHolding:
    """One position of a securities account statement (Depotauszug)."""

    name: str
    quantity: float
    unit: str  # "Stück", "Nominale"
    value_cents: int  # market value in the statement currency
    isin: str | None = None
    wkn: str | None = None
    price: float | None = None
    price_currency: str | None = None  # "EUR", "USD", "%"


@dataclass
class ParsedStatement:
    importer: str
    institution: str
    account_name: str
    account_kind: str  # see db.models.AccountKind
    account_iban: str | None
    currency: str = "EUR"
    statement_no: str | None = None
    statement_date: date | None = None
    period_start: date | None = None
    period_end: date | None = None
    opening_balance_cents: int | None = None
    closing_balance_cents: int | None = None
    transactions: list[ParsedTransaction] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # A valuation sets the account balance on ``period_end`` to ``closing_balance_cents``
    # instead of importing bookings: a securities account statement (holdings and their
    # total market value) or a balance statement (loan; its bookings only serve the check).
    valuation: bool = False
    holdings: list[ParsedHolding] = field(default_factory=list)
    links: list[ParsedLink] = field(default_factory=list)
    # Prefix of the dedupe keys instead of the IBAN / account name (an account without IBAN
    # whose name the user may change, e.g. the e-mail address of a PayPal account).
    dedupe_scope: str | None = None
    # also stored: a booking of the account with the same date and amount (see CsvFormat)
    match_by_date_amount: bool = False
    # Fetched by a connector: the bookings from ``period_start`` on (None: as far back as
    # the bank allows) as the bank lists them now, ``closing_balance_cents`` the balance on
    # ``period_end``. Fetches overlap; bookings already stored are skipped (no overwrite).
    incremental: bool = False

    @property
    def total_cents(self) -> int:
        return sum(t.amount_cents for t in self.transactions)

    @property
    def balance_checkable(self) -> bool:
        if self.holdings:
            return self.closing_balance_cents is not None
        return self.opening_balance_cents is not None and self.closing_balance_cents is not None

    @property
    def balance_ok(self) -> bool:
        """Opening balance + bookings (or: sum of holdings) equals the closing balance."""
        return self.balance_checkable and self.balance_difference_cents == 0

    @property
    def balance_difference_cents(self) -> int:
        closing, opening = self.closing_balance_cents, self.opening_balance_cents
        if closing is None or not self.balance_checkable:
            return 0
        if self.holdings:
            return closing - sum(h.value_cents for h in self.holdings)
        assert opening is not None  # balance_checkable
        return closing - (opening + self.total_cents)


class ImportError_(Exception):
    """A source file could not be parsed."""


class FileImporter(Protocol):
    """Reads one kind of statement file. ``name`` is stored with every import: never
    change it once files were imported with it."""

    name: str

    def detect(self, path: Path) -> bool:
        """Cheap check whether this importer understands the file."""
        ...

    def parse(self, path: Path) -> ParsedStatement: ...


def find_importer(path: Path, candidates: list[FileImporter] | None = None) -> FileImporter:
    """The first of ``candidates`` that recognizes the file; by default the file importers
    of the plugins last loaded (``importers.plugins``; the built-in ones before any
    import)."""
    if candidates is None:
        from hiro_budget.importers.plugins import current

        candidates = current().file_importers()
    for imp in candidates:
        if imp.detect(path):
            return imp
    raise ImportError_(f"no importer recognizes {path.name}")
