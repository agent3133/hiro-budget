"""SQLAlchemy ORM models. All money amounts are integer cents."""

from __future__ import annotations

import enum
from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# Stable constraint names so Alembic batch migrations work on SQLite.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def _enum(cls: type[enum.Enum]) -> Enum:
    """Store enum values (not member names) as plain strings."""
    return Enum(cls, native_enum=False, length=20, values_callable=lambda e: [m.value for m in e])


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class AccountKind(enum.StrEnum):
    GIRO = "giro"
    SAVINGS = "savings"
    CASH = "cash"
    DEPOT = "depot"
    LOAN = "loan"
    CREDIT_CARD = "credit_card"
    WALLET = "wallet"  # payment service such as PayPal


class SplitSource(enum.StrEnum):
    MANUAL = "manual"
    RULE = "rule"
    ML = "ml"
    MORTGAGE = "mortgage"
    TRANSFER = "transfer"
    UNCATEGORIZED = "uncategorized"


class Account(Base):
    __tablename__ = "account"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    kind: Mapped[AccountKind] = mapped_column(_enum(AccountKind))
    institution: Mapped[str | None] = mapped_column(String(120))
    iban: Mapped[str | None] = mapped_column(String(34), unique=True)
    currency: Mapped[str] = mapped_column(String(3), default="EUR")
    is_own: Mapped[bool] = mapped_column(Boolean, default=True)
    opening_balance_cents: Mapped[int | None] = mapped_column(Integer)
    opening_date: Mapped[date | None] = mapped_column(Date)
    on_budget: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")

    transactions: Mapped[list[Transaction]] = relationship(
        back_populates="account", foreign_keys="Transaction.account_id"
    )

    def __repr__(self) -> str:
        return f"Account({self.name!r}, {self.kind})"


class ImportBatch(Base):
    __tablename__ = "import_batch"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("account.id"))
    importer: Mapped[str] = mapped_column(String(60))
    source_file: Mapped[str] = mapped_column(String(500))
    file_sha256: Mapped[str] = mapped_column(String(64), unique=True)
    statement_no: Mapped[str | None] = mapped_column(String(40))
    statement_date: Mapped[date | None] = mapped_column(Date)
    period_start: Mapped[date | None] = mapped_column(Date)
    period_end: Mapped[date | None] = mapped_column(Date)
    opening_balance_cents: Mapped[int | None] = mapped_column(Integer)
    closing_balance_cents: Mapped[int | None] = mapped_column(Integer)
    imported_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    # Securities account statement: holdings on ``period_end`` worth ``closing_balance_cents``
    # (no bookings). The latest valuation sets the account's balance.
    valuation: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")

    account: Mapped[Account] = relationship()
    transactions: Mapped[list[Transaction]] = relationship(back_populates="batch")
    holdings: Mapped[list[Holding]] = relationship(
        back_populates="batch", cascade="all, delete-orphan", order_by="Holding.sort"
    )


class Holding(Base):
    """One position of a valuation (securities account statement)."""

    __tablename__ = "holding"

    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(
        ForeignKey("import_batch.id", ondelete="CASCADE"), index=True
    )
    sort: Mapped[int] = mapped_column(Integer, default=0)
    name: Mapped[str] = mapped_column(String(200))
    isin: Mapped[str | None] = mapped_column(String(12))
    wkn: Mapped[str | None] = mapped_column(String(6))
    quantity: Mapped[float] = mapped_column(Float)
    unit: Mapped[str] = mapped_column(String(20))
    price: Mapped[float | None] = mapped_column(Float)
    price_currency: Mapped[str | None] = mapped_column(String(3))
    value_cents: Mapped[int] = mapped_column(Integer)

    batch: Mapped[ImportBatch] = relationship(back_populates="holdings")


class CategoryGroup(Base):
    __tablename__ = "category_group"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(60), unique=True)
    name: Mapped[str] = mapped_column(String(120))
    sort: Mapped[int] = mapped_column(Integer, default=0)
    is_income: Mapped[bool] = mapped_column(Boolean, default=False)
    hidden: Mapped[bool] = mapped_column(Boolean, default=False)
    # Budgeted as one envelope instead of per category (``budget: group`` in YAML).
    budget_by_group: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")

    categories: Mapped[list[Category]] = relationship(
        back_populates="group", order_by="Category.sort"
    )


class Category(Base):
    __tablename__ = "category"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(121), unique=True)  # "group.category"
    group_id: Mapped[int] = mapped_column(ForeignKey("category_group.id"))
    name: Mapped[str] = mapped_column(String(120))
    sort: Mapped[int] = mapped_column(Integer, default=0)
    hidden: Mapped[bool] = mapped_column(Boolean, default=False)
    # For reports (``one_off`` / ``report_shift_days`` in YAML, see CategoryDef).
    one_off: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    report_shift_days: Mapped[int] = mapped_column(Integer, default=0, server_default="0")

    group: Mapped[CategoryGroup] = relationship(back_populates="categories")

    @property
    def full_name(self) -> str:
        return f"{self.group.name}: {self.name}"


class Transaction(Base):
    __tablename__ = "transaction"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("account.id"), index=True)
    batch_id: Mapped[int | None] = mapped_column(ForeignKey("import_batch.id"))
    booking_date: Mapped[date] = mapped_column(Date, index=True)
    value_date: Mapped[date | None] = mapped_column(Date)
    amount_cents: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3), default="EUR")
    booking_type: Mapped[str] = mapped_column(String(60))
    counterparty: Mapped[str] = mapped_column(String(200), default="")
    purpose: Mapped[str] = mapped_column(Text, default="")
    mandate_ref: Mapped[str | None] = mapped_column(String(80))
    reference: Mapped[str | None] = mapped_column(String(120))
    merchant: Mapped[str | None] = mapped_column(String(200))
    card_purchase_date: Mapped[date | None] = mapped_column(Date)
    raw_text: Mapped[str] = mapped_column(Text, default="")
    dedupe_key: Mapped[str] = mapped_column(String(64), unique=True)
    notes: Mapped[str | None] = mapped_column(Text)
    # Payee and article of a linked payment (PayPal) that this booking paid for.
    details: Mapped[str | None] = mapped_column(Text)
    # Set on automatically created counter-bookings of a transfer: the booking whose
    # transfer split created it. Deleted together with that booking.
    counterpart_of_id: Mapped[int | None] = mapped_column(
        ForeignKey("transaction.id", ondelete="CASCADE"), index=True
    )

    account: Mapped[Account] = relationship(
        back_populates="transactions", foreign_keys=[account_id]
    )
    batch: Mapped[ImportBatch | None] = relationship(back_populates="transactions")
    splits: Mapped[list[Split]] = relationship(
        back_populates="transaction", cascade="all, delete-orphan", order_by="Split.id"
    )
    counterpart_of: Mapped[Transaction | None] = relationship(
        remote_side=[id], back_populates="counterparts"
    )
    counterparts: Mapped[list[Transaction]] = relationship(
        back_populates="counterpart_of", cascade="all, delete-orphan"
    )

    @property
    def is_manual_entry(self) -> bool:
        """Entered by hand (or created as transfer counter-booking), not imported."""
        return self.batch_id is None

    @property
    def is_counterpart(self) -> bool:
        return self.counterpart_of_id is not None

    @property
    def is_transfer(self) -> bool:
        """True if the whole booking moves money between own accounts."""
        return bool(self.splits) and all(s.transfer_account is not None for s in self.splits)

    @property
    def is_manual(self) -> bool:
        return any(s.source == SplitSource.MANUAL for s in self.splits)

    @property
    def display_name(self) -> str:
        return self.merchant or self.counterparty


class Split(Base):
    __tablename__ = "split"

    id: Mapped[int] = mapped_column(primary_key=True)
    transaction_id: Mapped[int] = mapped_column(
        ForeignKey("transaction.id", ondelete="CASCADE"), index=True
    )
    category_id: Mapped[int | None] = mapped_column(ForeignKey("category.id"), index=True)
    # Set when this part of the booking moves money to/from another own account
    # (e.g. savings transfer, principal part of a mortgage payment). Such splits are
    # excluded from income/expense reports and have no category.
    transfer_account_id: Mapped[int | None] = mapped_column(ForeignKey("account.id"))
    amount_cents: Mapped[int] = mapped_column(Integer)
    memo: Mapped[str | None] = mapped_column(String(200))
    source: Mapped[SplitSource] = mapped_column(_enum(SplitSource), default=SplitSource.MANUAL)
    rule_id: Mapped[str | None] = mapped_column(String(80))
    confidence: Mapped[float | None] = mapped_column(Float)

    transaction: Mapped[Transaction] = relationship(back_populates="splits")
    category: Mapped[Category | None] = relationship()
    transfer_account: Mapped[Account | None] = relationship(foreign_keys=[transfer_account_id])

    @property
    def is_uncategorized(self) -> bool:
        return self.category is None and self.transfer_account is None


class LinkedPayment(Base):
    """A payment of a payment service (PayPal) that a bank booking paid for.

    The payment and the row funding it are not imported as bookings: the bank booking
    stays the expense and shows the payee and article (``Transaction.details``). Until
    that booking is imported, ``transaction_id`` is empty (waiting); after every import
    or fetch the waiting ones are matched again. A deleted bank booking (overwrite) sets
    it back to waiting.
    """

    __tablename__ = "linked_payment"
    __table_args__ = (
        UniqueConstraint("account_id", "external_id", name="uq_linked_payment_account_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("account.id"))  # the PayPal account
    batch_id: Mapped[int] = mapped_column(
        ForeignKey("import_batch.id", ondelete="CASCADE"), index=True
    )
    external_id: Mapped[str] = mapped_column(String(64))  # the payment's transaction code
    booking_date: Mapped[date] = mapped_column(Date)
    amount_cents: Mapped[int] = mapped_column(Integer)  # as the bank booking shows it
    payee: Mapped[str] = mapped_column(String(200), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    bank_match: Mapped[str] = mapped_column(String(200), default="")  # regex, see CsvFunding
    transaction_id: Mapped[int | None] = mapped_column(
        ForeignKey("transaction.id", ondelete="SET NULL"), index=True
    )

    account: Mapped[Account] = relationship()
    batch: Mapped[ImportBatch] = relationship()
    transaction: Mapped[Transaction | None] = relationship()

    @property
    def details(self) -> str:
        return " · ".join(part for part in (self.payee, self.description) if part)


class BudgetAssignment(Base):
    """Money assigned in a month (YNAB "Assigned") to a category or, for groups budgeted
    as a whole, to the group. Exactly one of ``category_id`` and ``group_id`` is set."""

    __tablename__ = "budget_assignment"
    __table_args__ = (
        UniqueConstraint("month", "category_id", name="uq_budget_assignment_month"),
        UniqueConstraint("month", "group_id", name="uq_budget_assignment_month_group"),
        CheckConstraint("(category_id IS NULL) != (group_id IS NULL)", name="category_or_group"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    month: Mapped[str] = mapped_column(String(7), index=True)  # "YYYY-MM"
    category_id: Mapped[int | None] = mapped_column(ForeignKey("category.id"))
    group_id: Mapped[int | None] = mapped_column(ForeignKey("category_group.id"))
    amount_cents: Mapped[int] = mapped_column(Integer)

    category: Mapped[Category | None] = relationship()
    group: Mapped[CategoryGroup | None] = relationship()
