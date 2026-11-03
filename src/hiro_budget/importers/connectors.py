"""Connectors: plugins that fetch bookings from a bank instead of reading a file (#82).

A connector is the second kind of importer of plugin API 2. It returns the same
``ParsedStatement`` as a file importer (with ``incremental`` set), and the app stores it
through the same pipeline (``services.import_service``). What differs is the input: a
connection with settings, a secret the user types at every connection (a PIN, never stored)
and confirmations the bank may want on the way (TAN, app). The app owns all of the user
interface: it renders a form from the connector's settings models and asks for the secret
and confirmations; connector code never touches Qt.

The app calls ``connect`` on a worker thread::

    with connector.connect(login, options, secret, confirm) as session:
        for account in session.accounts():
            statement = session.fetch(account, start)

and raises nothing but ``ConnectorError`` and its subclasses to the user.
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Protocol

from pydantic import BaseModel

from hiro_budget.importers.base import ParsedStatement


class ConnectorError(Exception):
    """The bank could not be reached or refused a request."""


class LoginError(ConnectorError):
    """The bank rejected the login (e.g. login ID or PIN wrong): the app asks again."""


class Cancelled(ConnectorError):
    """The user cancelled a confirmation."""


class Offline(ConnectorError):
    """No connection to the bank's server."""


@dataclass
class RemoteAccount:
    """An account of a login as the bank reports it."""

    bank_id: str  # IBAN, or the depot number of a securities account (as on its statements)
    product: str  # e.g. "Girokonto", "Extra-Konto", "Direkt-Depot"
    kind: str  # AccountKind value: giro, savings, depot, loan, credit_card
    iban: str | None = None
    account_number: str | None = None
    # what the connector needs to address the account again (opaque to the app)
    handle: object = field(default=None, repr=False, compare=False)


class Confirmation(Protocol):
    """How a connector asks the user when the bank wants a confirmation (called from the
    worker thread)."""

    def tan(self, challenge: str) -> str | None:
        """A TAN for ``challenge``; None: cancelled."""
        ...

    def wait_for_app(self, challenge: str) -> bool:
        """The bank waits for a confirmation in its app: wait a moment (the connector asks
        the bank again afterwards) and return False when the user cancelled."""
        ...


class ConnectorSession(Protocol):
    def accounts(self) -> list[RemoteAccount]: ...

    def fetch(self, account: RemoteAccount, start: date | None) -> ParsedStatement:
        """Bookings from ``start`` on (None: as far back as the bank allows) and the
        current balance, as an ``incremental`` statement."""
        ...


class Connector(Protocol):
    """A bank connection type.

    ``name`` is stored with every fetch (as the importer of its import batch): never change
    it. ``login_model`` holds the settings of one login (pydantic, without the secret), and
    ``options_model`` settings shared by all logins of this connector (or None). Their
    fields' ``title`` and ``description`` label the settings form; ``texts`` translates
    them, and ``secret``, per language (``{"de": {"Bank code": "Bankleitzahl"}}``).
    """

    name: str
    title: str  # e.g. "FinTS": in the settings and in the import history
    secret: str  # what the user types at every connection, e.g. "PIN"; "" for nothing
    login_model: type[BaseModel]
    options_model: type[BaseModel] | None
    texts: dict[str, dict[str, str]]

    def connect(
        self, login: Any, options: Any, secret: str, confirm: Confirmation
    ) -> AbstractContextManager[ConnectorSession]: ...


def suggestions(connector: Connector, values: dict[str, str]) -> dict[str, str]:
    """Values the connector derives from the login's settings while they are typed in
    (optional method ``suggest``): settings fields, e.g. the server of a known bank code,
    and ``name``, a name for the login (the bank's)."""
    suggest = getattr(connector, "suggest", None)
    return dict(suggest(values)) if suggest is not None else {}


def error_message(error: Any) -> str:
    """The message of one pydantic error: a validator's own text without pydantic's
    "Value error, " prefix (``texts`` may translate it)."""
    if error.get("type") == "value_error" and "error" in error.get("ctx", {}):
        return str(error["ctx"]["error"])
    return str(error.get("msg", ""))


def text(connector: Connector, english: str, language: str) -> str:
    """A connector's label in the app's language."""
    return connector.texts.get(language, {}).get(english, english)
