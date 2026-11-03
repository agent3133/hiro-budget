"""The connector session on the ``fints`` library (FinTS 3.0, PIN/TAN).

Imported only when the user fetches or tests a connection. MT940 bookings are turned into
``ParsedTransaction`` the way the PDF importer would read them: whitespace collapsed, the
FinTS booking-type names mapped to the names on the statements (rules match them).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING, Any, cast

from fints.client import FinTS3PinTanClient, NeedTANResponse
from fints.exceptions import (
    FinTSClientPINError,
    FinTSClientTemporaryAuthError,
    FinTSConnectionError,
    FinTSError,
)

from hiro_budget.plugin_api import (
    Cancelled,
    Confirmation,
    ConnectorError,
    LoginError,
    Offline,
    ParsedStatement,
    ParsedTransaction,
    RemoteAccount,
)

if TYPE_CHECKING:
    from .connector import FintsLogin

log = logging.getLogger(__name__)

# python-fints logs every message it sends and receives on DEBUG (complete statements with
# IBANs, names, purposes, balances): never, even when the app logs on DEBUG (#75)
_library_log = logging.getLogger("fints")
if _library_log.getEffectiveLevel() < logging.INFO:
    _library_log.setLevel(logging.INFO)

# FinTS booking texts that are named differently on the PDF statements (spike, issue #6)
BOOKING_TYPES = {
    "Lastschrifteinzug": "Lastschrift",
    "Dauerauftrag/Terminueberweisung": "Dauerauftrag/Terminueberw.",
    "Gutschrift aus Dauerauftrag": "Gutschrift/Dauerauftrag",
}

_WS = re.compile(r"\s+")


def clean(text: str | list | None) -> str:
    if isinstance(text, list):
        text = " ".join(t for t in text if t)
    return _WS.sub(" ", text or "").strip()


# product names when the bank gives no account type (ING: type is empty for all accounts)
_PRODUCT_KINDS = (
    ("depot", "depot"),
    ("extra", "savings"),
    ("spar", "savings"),
    ("tagesgeld", "savings"),
    ("festgeld", "savings"),
    ("kreditkarte", "credit_card"),
    ("visa", "credit_card"),
    ("kredit", "loan"),
    ("finanzierung", "loan"),
    ("darlehen", "loan"),
)


def kind_of(account_type: int | None, product: str = "") -> str:
    """AccountKind for the FinTS account type (UPD: 1-9 current account, 10-19 savings,
    20-29 fixed deposit, 30-39 securities, 40-49 loan, 50-59 credit card, 60-69 funds), else
    from the product name."""
    if account_type is None:
        name = product.lower()
        return next((kind for word, kind in _PRODUCT_KINDS if word in name), "giro")
    if 10 <= account_type <= 29:
        return "savings"
    if 30 <= account_type <= 39 or 60 <= account_type <= 69:
        return "depot"
    if 40 <= account_type <= 49:
        return "loan"
    if 50 <= account_type <= 59:
        return "credit_card"
    return "giro"


def cents(amount) -> int:
    """mt940 Amount (or Decimal) in cents."""
    value = getattr(amount, "amount", amount)
    return int((Decimal(value) * 100).quantize(Decimal(1)))


def _ref(value: str | None) -> str | None:
    value = clean(value)
    return value if value and value.upper() != "NONREF" else None


def to_parsed(data: dict, enrich=None) -> ParsedTransaction:
    """One MT940 booking (``Transaction.data``) as the statements would show it;
    ``enrich``: the bank's enricher (merchant, purchase date), see ``plugin_api``."""
    counterparty = clean(data.get("applicant_name"))
    purpose = clean(data.get("purpose"))
    booking = data.get("entry_date") or data.get("date")
    booking_type = clean(data.get("posting_text"))
    merchant, purchase = enrich(counterparty, purpose, booking) if enrich else (None, None)
    return ParsedTransaction(
        booking_date=cast("date", booking),  # MT940 bookings always have a date
        value_date=data.get("date"),
        amount_cents=cents(data["amount"]),
        booking_type=BOOKING_TYPES.get(booking_type, booking_type),
        counterparty=counterparty,
        purpose=purpose,
        mandate_ref=_ref(data.get("additional_position_reference")),
        reference=_ref(data.get("end_to_end_reference")),
        merchant=merchant,
        card_purchase_date=purchase,
        raw_text="FinTS",
    )


class _Session:
    def __init__(
        self, client: Any, confirm: Confirmation | None, bank_code: str | None = None
    ) -> None:
        from hiro_budget.plugin_api import enricher_for

        self.client = client
        self.confirm = confirm
        self.enrich = enricher_for(bank_code=bank_code) if bank_code else None

    def answer(self, result):
        """Confirm until the bank answers (app confirmation or TAN)."""
        while isinstance(result, NeedTANResponse):
            assert self.confirm is not None
            challenge = clean(result.challenge) or ""
            if result.decoupled:
                if not self.confirm.wait_for_app(challenge):
                    raise Cancelled("cancelled")
                result = self.client.send_tan(result, "")
            else:
                tan = self.confirm.tan(challenge)
                if tan is None:
                    raise Cancelled("cancelled")
                result = self.client.send_tan(result, tan)
        return result

    def accounts(self) -> list[RemoteAccount]:
        info = self.client.get_information()
        details = {}
        for a in info.get("accounts", []):
            details[a.get("iban") or a.get("account_number")] = a
        result = []
        for sepa in self.answer(self.client.get_sepa_accounts()) or []:
            d = details.get(sepa.iban) or details.get(sepa.accountnumber) or {}
            product = clean(d.get("product_name")) or "Konto"
            kind = kind_of(d.get("type"), product)
            iban = (sepa.iban or "").replace(" ", "").upper() or None
            number = (sepa.accountnumber or "").strip() or None
            # depots have an IBAN at ING too, but their statements carry the depot number
            bank_id = (number if kind == "depot" else iban) or iban or number or ""
            result.append(
                RemoteAccount(
                    bank_id=bank_id,
                    product=product,
                    kind=kind,
                    iban=iban,
                    account_number=number,
                    handle=sepa,
                )
            )
        return result

    def fetch(self, account: RemoteAccount, start: date | None) -> ParsedStatement:
        balance = self.answer(self.client.get_balance(account.handle))
        booked = self.answer(self.client.get_transactions(account.handle, start, date.today()))
        return ParsedStatement(
            importer="fints",
            institution="",  # the app names it after the login
            account_name=account.product,
            account_kind=account.kind,
            account_iban=account.bank_id,
            period_start=start,
            period_end=balance.date if balance is not None else None,
            closing_balance_cents=cents(balance.amount) if balance is not None else None,
            transactions=[to_parsed(t.data, self.enrich) for t in booked or []],
            incremental=True,
        )


def _bootstrap(client: FinTS3PinTanClient) -> None:
    """Pick the TAN procedure (and medium) when the bank needs one."""
    if not client.get_current_tan_mechanism():
        client.fetch_tan_mechanisms()
        mechanisms = list(client.get_tan_mechanisms().items())
        if mechanisms:
            # prefer a confirmation in the app (decoupled) over typing a TAN
            decoupled = [k for k, m in mechanisms if getattr(m, "decoupled_max_poll_number", None)]
            client.set_tan_mechanism(decoupled[0] if decoupled else mechanisms[0][0])
    if client.selected_tan_medium is None and client.is_tan_media_required():
        media = client.get_tan_media()[1]
        if media:
            client.set_tan_medium(media[0])
        else:  # as the library's own bootstrap: some banks accept an empty name
            client.selected_tan_medium = ""


@contextmanager
def session(
    login: FintsLogin, pin: str, product_id: str, confirm: Confirmation
) -> Iterator[_Session]:
    try:
        client = FinTS3PinTanClient(
            login.bank_code, login.login_id, pin, login.server, product_id=product_id
        )
        _bootstrap(client)
        with client:
            s = _Session(client, confirm, login.bank_code)
            if client.init_tan_response:
                s.answer(client.init_tan_response)
            yield s
    except (FinTSClientPINError, FinTSClientTemporaryAuthError) as e:
        raise LoginError(str(e)) from e
    except FinTSConnectionError as e:
        raise Offline(str(e)) from e
    except FinTSError as e:
        raise ConnectorError(str(e)) from e
