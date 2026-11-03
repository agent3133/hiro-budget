"""The FinTS connector: settings of a login, the product ID, and the connection.

The ``fints`` library is imported only when connecting (``client``), so the app starts
without it. The PIN is the connector's secret: the app asks for it at every connection and
never stores it.
"""

from __future__ import annotations

from contextlib import AbstractContextManager

from pydantic import BaseModel, ConfigDict, Field, field_validator

from hiro_budget.plugin_api import Confirmation, ConnectorSession

# Sent while no product ID is registered (accepted by ING in the spike, issue #6).
PLACEHOLDER_PRODUCT_ID = "LOCALBUDGETSPIKE"

# FinTS servers of banks the connector knows (bank code -> URL, bank); others are typed in
BANKS = {"50010517": ("https://fints.ing.de/fints/", "ING")}
SERVERS = {code: server for code, (server, _bank) in BANKS.items()}


class FintsLogin(BaseModel):
    """One online banking access (the PIN is not part of it)."""

    model_config = ConfigDict(extra="forbid")

    bank_code: str = Field("50010517", min_length=1, title="Bank code")
    server: str = Field(SERVERS["50010517"], title="FinTS server")
    login_id: str = Field(
        min_length=1,
        title="Login ID",
        description="e.g. the access number of your online banking",
    )

    @field_validator("bank_code", "login_id", mode="before")
    @classmethod
    def _text(cls, v: object) -> object:
        return str(v).strip() if isinstance(v, (str, int)) else v  # "bank_code: 50010517"

    @field_validator("server")
    @classmethod
    def _https_only(cls, v: str) -> str:
        """Login ID and PIN go to this server: never unencrypted (#75)."""
        v = v.strip()
        if not v.lower().startswith("https://"):
            raise ValueError("the server address must start with https://")
        return v


class FintsOptions(BaseModel):
    """Settings for all FinTS logins."""

    model_config = ConfigDict(extra="forbid")

    product_id: str | None = Field(
        None,
        title="Product ID",
        description=(
            "Without a registered product ID the app sends a placeholder; the bank may stop "
            "accepting it. Register the app with the Deutsche Kreditwirtschaft and enter the "
            "ID here."
        ),
    )


class FintsConnector:
    name = "fints"  # the importer of every fetch: never change it
    title = "FinTS"
    secret = "PIN"
    login_model = FintsLogin
    options_model = FintsOptions
    texts = {
        "de": {
            "Bank code": "Bankleitzahl",
            "FinTS server": "FinTS-Server",
            "Login ID": "Anmeldename",
            "e.g. the access number of your online banking": (
                "z. B. die Zugangsnummer des Onlinebankings"
            ),
            "Product ID": "Produkt-ID",
            "the server address must start with https://": (
                "die Serveradresse muss mit https:// beginnen"
            ),
            "Without a registered product ID the app sends a placeholder; the bank may stop "
            "accepting it. Register the app with the Deutsche Kreditwirtschaft and enter the "
            "ID here.": (
                "Ohne registrierte Produkt-ID sendet die App einen Platzhalter; die Bank kann "
                "ihn künftig ablehnen. Die App bei der Deutschen Kreditwirtschaft registrieren "
                "und die ID hier eintragen."
            ),
        }
    }

    def suggest(self, values: dict[str, str]) -> dict[str, str]:
        """The server and the login's name for a bank code the connector knows."""
        code = (values.get("bank_code") or "").strip()
        if code not in BANKS:
            return {}
        server, bank = BANKS[code]
        return {"server": server, "name": bank}

    def connect(
        self,
        login: FintsLogin,
        options: FintsOptions | None,
        secret: str,
        confirm: Confirmation,
    ) -> AbstractContextManager[ConnectorSession]:
        from . import client  # the fints library: only now

        product = (options.product_id if options else None) or PLACEHOLDER_PRODUCT_ID
        return client.session(login, secret, product, confirm)
