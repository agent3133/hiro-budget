"""Online banking access (FinTS): the app's own interface to a bank.

``connect`` opens a session with a bank login; the implementation (``fints_client``, on
the ``fints`` library) is imported only then, so the app starts without it. Tests replace
``connect`` with a fake bank. Nothing here touches the database: ``services.bank.fetch``
imports what a session returns.
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import date
from typing import Protocol

from hiro_budget.config import BankLogin
from hiro_budget.importers.base import ParsedTransaction

# Sent while no product ID is registered (accepted by ING in the spike, issue #6).
PLACEHOLDER_PRODUCT_ID = "LOCALBUDGETSPIKE"


class BankError(Exception):
    """The bank could not be reached or refused a request."""


class PinError(BankError):
    """The bank rejected the login ID or PIN."""


class Cancelled(BankError):
    """The user cancelled a confirmation."""


class Offline(BankError):
    """No connection to the bank's server."""


@dataclass
class BankAccount:
    """An account of a bank login as the bank reports it."""

    bank_id: str  # IBAN, or the depot number of a securities account (as on its statements)
    product: str  # e.g. "Girokonto", "Extra-Konto", "Direkt-Depot"
    kind: str  # AccountKind value: giro, savings, depot, loan, credit_card
    iban: str | None = None
    account_number: str | None = None
    # what fints needs to address the account again (opaque to everyone else)
    handle: object = field(default=None, repr=False, compare=False)


@dataclass
class Fetched:
    """Bookings and balance of one account."""

    account: BankAccount
    start: date | None  # first day asked for (None: as far back as the bank allows)
    balance_cents: int | None
    balance_date: date | None
    transactions: list[ParsedTransaction]


class Confirmation(Protocol):
    """How a bank session asks the user when the bank wants a confirmation.

    Called from the thread that talks to the bank.
    """

    def tan(self, challenge: str) -> str | None:
        """A TAN for ``challenge``; None: cancelled."""
        ...

    def wait_for_app(self, challenge: str) -> bool:
        """The bank waits for a confirmation in its app: wait a moment (the session asks
        the bank again afterwards) and return False when the user cancelled."""
        ...


class BankSession(Protocol):
    def accounts(self) -> list[BankAccount]: ...

    def fetch(self, account: BankAccount, start: date | None) -> Fetched: ...


def product_id(configured: str | None) -> str:
    return configured or PLACEHOLDER_PRODUCT_ID


def connect(
    login: BankLogin, pin: str, product: str | None, confirm: Confirmation
) -> AbstractContextManager[BankSession]:
    """A session with the bank (``with connect(...) as bank: ...``)."""
    from hiro_budget.services.bank import fints_client

    return fints_client.session(login, pin, product_id(product), confirm)
