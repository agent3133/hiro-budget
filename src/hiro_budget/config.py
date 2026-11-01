"""Loading, validation and saving of the YAML configuration files.

The config directory holds three files: ``config.yaml``, ``categories.yaml`` and
``rules.yaml``. On first start the directory and each missing file are created from the
``*.example.yaml`` templates shipped in the package (``TEMPLATES``). CSV
formats (``CsvFormat``) come from plugins (``importers.plugins``); the former
``csv_formats.yaml`` is migrated to plugins once (``importers.plugins.migrate_csv_formats``).
Files are read with ruamel.yaml in round-trip mode so that saving from the GUI keeps
the user's comments and ordering.
"""

from __future__ import annotations

import datetime
import os
import re
import shutil
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from ruamel.yaml import YAML

AccountKind = Literal["giro", "savings", "cash", "depot", "loan", "credit_card", "wallet"]
BUDGET_KINDS = frozenset({"giro", "savings", "cash", "credit_card", "wallet"})

CONFIG_DIR_ENV = "HIRO_BUDGET_CONFIG_DIR"
CONFIG_FILES = ("config.yaml", "categories.yaml", "rules.yaml")
TEMPLATES = Path(__file__).resolve().parent / "templates"  # <name>.example.yaml
CSV_FORMATS_FILE = "csv_formats.yaml"  # until 2026-11: migrated to plugins at start


class ConfigError(Exception):
    """Raised when a configuration file is missing or invalid."""


# --- config.yaml -----------------------------------------------------------------


class PatternMatch(BaseModel):
    """Regex patterns matched against a booking (all given patterns must match)."""

    counterparty: str | None = None
    purpose: str | None = None


class OwnAccount(BaseModel):
    name: str
    kind: AccountKind = "giro"
    institution: str | None = None
    iban: str | None = None
    # E-mail address of a payment service account (PayPal): CSV exports find it by it.
    email: str | None = None
    match: PatternMatch | None = None
    # Balance on opening_date (EUR); bookings from that date on are added to it.
    opening_balance: float | None = None
    opening_date: date | None = None
    # Money in budget accounts must be assigned to categories. None: default by kind
    # (giro, savings, cash, credit card are budget accounts; depot and loan are tracking).
    on_budget: bool | None = None
    # Budget category for money moved from a budget account into this account when it
    # is a tracking account (e.g. ETF purchases into the depot). Without it, such
    # transfers show up as uncategorized outflow.
    transfer_category: str | None = None

    @property
    def is_on_budget(self) -> bool:
        return self.on_budget if self.on_budget is not None else self.kind in BUDGET_KINDS

    @model_validator(mode="after")
    def _opening_complete(self) -> OwnAccount:
        if (self.opening_balance is None) != (self.opening_date is None):
            raise ValueError(
                f"account '{self.name}': opening_balance and opening_date must be given together"
            )
        return self

    @field_validator("iban")
    @classmethod
    def _normalize_iban(cls, v: str | None) -> str | None:
        return normalize_iban(v) if v else None


class MortgageConfig(BaseModel):
    name: str
    loan_account: str
    interest_category: str
    match: PatternMatch
    # Budget category for the principal (a transfer to the loan account, which is a
    # tracking account). Without it, the principal shows up as uncategorized outflow.
    principal_category: str | None = None


class MLConfig(BaseModel):
    enabled: bool = True
    min_samples: int = 30
    min_confidence: float = Field(0.6, ge=0.0, le=1.0)
    model_path: str = "data/classifier.npz"


class BudgetConfig(BaseModel):
    # First budget month "YYYY-MM". None: month of the first booking on a budget account.
    start_month: str | None = Field(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    # Budget proposals from history: months considered for regular categories and
    # rounding of proposals (up to a multiple of this many euros; 0 = exact).
    proposal_months: int = Field(6, ge=1, le=12)
    proposal_round_to: int = Field(10, ge=0, le=1000)


class MatchingConfig(BaseModel):
    # An imported booking replaces a manual entry on the same account with the same
    # amount whose date differs by at most this many days.
    days: int = Field(7, ge=0, le=60)


class BackupConfig(BaseModel):
    # Copy database and configuration before every import; keep the newest backups.
    before_import: bool = True
    keep: int = Field(10, ge=1, le=100)


class DatabaseConfig(BaseModel):
    path: str = "data/finance.db"


class LoggingConfig(BaseModel):
    level: str = "INFO"


class BankAccountLink(BaseModel):
    """An account of a bank login and the app account its bookings go to."""

    bank_id: str  # IBAN, or the depot number for securities accounts (no IBAN)
    product: str = ""  # the bank's product name, e.g. "Girokonto"
    kind: AccountKind = "giro"
    account: str  # app account name
    # First day fetched ("cut-over"): statements cover the days before. None: as far back
    # as the bank allows.
    fetch_from: date | None = None

    @field_validator("bank_id")
    @classmethod
    def _normalize_id(cls, v: str) -> str:
        return normalize_iban(v)


class BankLogin(BaseModel):
    """Online banking access via FinTS (PIN/TAN). The PIN is never stored here."""

    name: str = "ING"
    bank_code: str = "50010517"
    server: str = "https://fints.ing.de/fints/"
    login_id: str
    accounts: list[BankAccountLink] = []


class FintsConfig(BaseModel):
    # Product ID registered with the Deutsche Kreditwirtschaft. Empty: a placeholder is
    # sent (the bank may stop accepting unregistered clients).
    product_id: str | None = None
    logins: list[BankLogin] = []


class PluginsConfig(BaseModel):
    # User plugins (config/plugins/) with Python code run only when listed here; plugins
    # with CSV formats only, and the built-in ones, need no enabling.
    enabled: list[str] = []


class AppConfig(BaseModel):
    database: DatabaseConfig = DatabaseConfig()
    language: Literal["de", "en"] = "de"
    theme: Literal["system", "light", "dark", "rose_pine", "rose_pine_dark"] = "system"  # look
    logging: LoggingConfig = LoggingConfig()
    # where new statements are saved; PDFs in it not imported yet are offered for import
    import_folder: str = "input"
    own_accounts: list[OwnAccount] = []
    mortgage: list[MortgageConfig] = []
    ml: MLConfig = MLConfig()
    matching: MatchingConfig = MatchingConfig()
    budget: BudgetConfig = BudgetConfig()
    backup: BackupConfig = BackupConfig()
    fints: FintsConfig = FintsConfig()
    plugins: PluginsConfig = PluginsConfig()

    @field_validator("theme", mode="before")
    @classmethod
    def _former_theme_names(cls, value: Any) -> Any:
        # "Rosé Pine" was called "Obsidian" up to 0.1.0
        return {"obsidian": "rose_pine", "obsidian_dark": "rose_pine_dark"}.get(value, value)

    @model_validator(mode="after")
    def _unique_account_names(self) -> AppConfig:
        names = [a.name for a in self.own_accounts]
        dupes = {n for n in names if names.count(n) > 1}
        if dupes:
            raise ValueError(f"duplicate account names: {', '.join(sorted(dupes))}")
        return self

    @model_validator(mode="after")
    def _check_mortgage_accounts(self) -> AppConfig:
        names = {a.name for a in self.own_accounts}
        for m in self.mortgage:
            if m.loan_account not in names:
                raise ValueError(
                    f"mortgage '{m.name}': loan_account '{m.loan_account}' "
                    "is not listed under own_accounts"
                )
        return self


# --- categories.yaml -------------------------------------------------------------


TargetType = Literal["monthly", "by_date", "repeating", "balance"]

MAX_REPORT_SHIFT_DAYS = 15  # less than the shortest month: a shift moves one month at most


class TargetDef(BaseModel):
    """Budget target of a category.

    monthly:   assign ``amount`` every month
    by_date:   have ``amount`` available by ``date``
    repeating: have ``amount`` available every ``every_months`` months, first due ``date``
    balance:   build up to ``amount`` (by ``date`` if given, otherwise no deadline)
    """

    type: TargetType
    amount: float = Field(gt=0)
    date: datetime.date | None = None  # module-qualified: the field name shadows the type
    every_months: int = Field(12, ge=1, le=120)

    @model_validator(mode="after")
    def _date_required(self) -> TargetDef:
        if self.type in ("by_date", "repeating") and self.date is None:
            raise ValueError(f"target type '{self.type}' needs a date")
        return self


class CategoryDef(BaseModel):
    key: str
    name: str
    target: TargetDef | None = None
    # Reports only (the budget stays on booking dates): irregular income or expenses that
    # the reports can leave out, and bookings on day <= report_shift_days that count for
    # the previous month (e.g. a month-end instalment debited on the 1st).
    one_off: bool = False
    report_shift_days: int = Field(0, ge=0, le=MAX_REPORT_SHIFT_DAYS)


BudgetLevel = Literal["category", "group"]


class GroupDef(BaseModel):
    key: str
    name: str
    income: bool = False
    # "group": the group is budgeted as one envelope (assigned, available and target on
    # the group); its categories are only used for categorization and show their activity.
    budget: BudgetLevel = "category"
    target: TargetDef | None = None  # only with budget: group
    categories: list[CategoryDef] = []

    @model_validator(mode="after")
    def _group_budget(self) -> GroupDef:
        if self.income and self.budget == "group":
            raise ValueError(f"group '{self.key}': income groups cannot be budgeted")
        if self.target is not None and self.budget != "group":
            raise ValueError(f"group '{self.key}': a group target needs 'budget: group'")
        return self

    @property
    def budget_by_group(self) -> bool:
        return self.budget == "group"


class CategoriesConfig(BaseModel):
    groups: list[GroupDef] = []

    @model_validator(mode="after")
    def _unique_keys(self) -> CategoriesConfig:
        seen: set[str] = set()
        for key in self.full_keys():
            if key in seen:
                raise ValueError(f"duplicate category key '{key}'")
            seen.add(key)
        return self

    def full_keys(self) -> list[str]:
        return [f"{g.key}.{c.key}" for g in self.groups for c in g.categories]


# --- rules.yaml ------------------------------------------------------------------


class RuleMatch(BaseModel):
    counterparty: str | None = None
    merchant: str | None = None
    purpose: str | None = None
    booking_type: str | None = None
    details: str | None = None  # payee and article of a linked PayPal payment
    account: str | None = None
    amount_min: float | None = None
    amount_max: float | None = None

    @model_validator(mode="after")
    def _not_empty(self) -> RuleMatch:
        if all(v is None for v in self.model_dump().values()):
            raise ValueError("rule match must contain at least one field")
        return self


class RuleDef(BaseModel):
    id: str
    category: str
    match: RuleMatch


class RulesConfig(BaseModel):
    rules: list[RuleDef] = []

    @model_validator(mode="after")
    def _unique_ids(self) -> RulesConfig:
        ids = [r.id for r in self.rules]
        dupes = {i for i in ids if ids.count(i) > 1}
        if dupes:
            raise ValueError(f"duplicate rule ids: {', '.join(sorted(dupes))}")
        return self


# --- CSV formats (*.csv.yaml in plugins) -----------------------------------------

ColumnRef = str | int  # a column by its header name (first one of that name) or its index


class CsvFilter(BaseModel):
    """Rows whose ``column`` equals ``equals`` or one of ``in``, or matches the regex
    ``matches``."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    column: ColumnRef
    equals: str | None = None
    in_: list[str] = Field(default=[], alias="in")
    matches: str | None = None

    @model_validator(mode="after")
    def _value_given(self) -> CsvFilter:
        if self.equals is None and not self.in_ and self.matches is None:
            raise ValueError(f"filter on '{self.column}' needs 'equals', 'in' or 'matches'")
        if self.matches is not None:
            re.compile(self.matches)
        return self

    def values(self) -> set[str]:
        return {v for v in (self.equals, *self.in_) if v is not None}


class CsvDetect(BaseModel):
    model_config = ConfigDict(extra="forbid")  # a misspelt key must not be ignored

    header_has: list[str] = Field(min_length=1)  # column names the header row contains


class CsvAccount(BaseModel):
    """Which account the file belongs to (one of them)."""

    model_config = ConfigDict(extra="forbid")  # a misspelt key must not be ignored

    name: str | None = None  # a fixed account
    iban_from: str | None = None  # "key;value" line above the header holding the IBAN
    name_from: str | None = None  # such a line holding the account name (after institution)
    email_column: str | None = None  # own account whose email is the sender of payments

    @model_validator(mode="after")
    def _one_given(self) -> CsvAccount:
        if not (self.name or self.iban_from or self.name_from or self.email_column):
            raise ValueError("account needs name, iban_from, name_from or email_column")
        return self


class CsvColumns(BaseModel):
    model_config = ConfigDict(extra="forbid")  # a misspelt key must not be ignored

    booking_date: ColumnRef
    amount: ColumnRef
    value_date: ColumnRef | None = None
    counterparty: ColumnRef | None = None
    booking_type: ColumnRef | None = None
    purpose: list[ColumnRef] = []  # joined with spaces
    reference: ColumnRef | None = None
    merchant: ColumnRef | None = None
    fee: ColumnRef | None = None  # charged on top of the amount: the booking is amount - fee
    balance: ColumnRef | None = None  # running balance after the row: balance check
    id: ColumnRef | None = None  # unique transaction ID: dedupe key (else the fingerprint)

    @field_validator("purpose", mode="before")
    @classmethod
    def _one_or_more(cls, v: Any) -> Any:
        return [v] if isinstance(v, (str, int)) else v


class CsvFunding(BaseModel):
    """Rows that move money between a bank account and this one for a payment (PayPal
    pulling it from the Giro). Such a payment is not imported: the bank booking stays the
    expense and gets the payee and article; the rest of a partly funded payment is."""

    model_config = ConfigDict(extra="forbid")  # a misspelt key must not be ignored

    rows: CsvFilter
    linked_by: ColumnRef  # column of a funding row naming the payment's ``id``
    bank_match: str = ""  # regex the bank booking's counterparty or purpose must match


class CsvFormat(BaseModel):
    model_config = ConfigDict(extra="forbid")  # a misspelt key must not be ignored

    name: str
    institution: str = ""
    kind: AccountKind = "giro"
    detect: CsvDetect
    encoding: str = "utf-8-sig"
    delimiter: str = Field(",", min_length=1, max_length=1)
    date_format: str = "%d.%m.%Y"
    decimal_comma: bool = True  # 1.234,56 (else 1,234.56)
    account: CsvAccount
    columns: CsvColumns
    # value renames per field, e.g. booking_type: {Überweisung: Ueberweisung}
    values: dict[str, dict[str, str]] = {}
    only: list[CsvFilter] = []  # rows to read at all: matching every one of them
    skip: list[CsvFilter] = []  # rows to leave out, e.g. pending ones
    funding: CsvFunding | None = None
    # institution or bank code whose enricher (a plugin's) derives merchant and card
    # purchase date from the texts, e.g. ING
    enrich: str | None = None
    # how a booking is recognised as already stored: by its fingerprint (texts, or the id
    # column), or also by date and amount (another source of the same account, e.g. a PDF
    # statement whose texts differ)
    match_by: Literal["fingerprint", "date_amount"] = "fingerprint"


class CsvFormatsConfig(BaseModel):
    """The former ``csv_formats.yaml`` (read only to migrate it to plugins)."""

    model_config = ConfigDict(extra="forbid")  # a misspelt key must not be ignored

    formats: list[CsvFormat] = []

    @model_validator(mode="after")
    def _unique_names(self) -> CsvFormatsConfig:
        names = [f.name for f in self.formats]
        dupes = {n for n in names if names.count(n) > 1}
        if dupes:
            raise ValueError(f"duplicate format names: {', '.join(sorted(dupes))}")
        return self


# --- helpers ---------------------------------------------------------------------


def normalize_iban(iban: str) -> str:
    return iban.replace(" ", "").upper()


def _yaml() -> YAML:
    y = YAML()  # round-trip mode keeps comments
    y.preserve_quotes = True
    y.indent(mapping=2, sequence=4, offset=2)
    y.width = 120
    return y


def load_yaml(path: Path) -> Any:
    with path.open(encoding="utf-8") as f:
        return _yaml().load(f) or {}


def save_yaml(path: Path, data: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        _yaml().dump(data, f)
    os.replace(tmp, path)


def default_config_dir() -> Path:
    """``$HIRO_BUDGET_CONFIG_DIR``, else ``./config``; the built exe started from elsewhere
    (Explorer, a shortcut) looks for ``config`` next to it and in the folders above."""
    env = os.environ.get(CONFIG_DIR_ENV)
    if env:
        return Path(env)
    here = Path.cwd() / "config"
    compiled = "__compiled__" in globals()  # set by Nuitka
    if here.is_dir() or not compiled:
        return here
    exe = Path(sys.argv[0]).resolve().parent
    for folder in (exe, *exe.parents):
        if (folder / "config").is_dir():
            return folder / "config"
    return here


def ensure_config_files(config_dir: Path) -> list[str]:
    """Create the config directory and missing config files from the templates (first
    start). Returns the names of the files created."""
    try:
        config_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise ConfigError(f"cannot create the config directory {config_dir}: {e}") from e
    created = []
    for name in CONFIG_FILES:
        target = config_dir / name
        if target.exists():
            continue
        template = TEMPLATES / name.replace(".yaml", ".example.yaml")
        if not template.exists():
            raise ConfigError(f"{target} is missing and the template {template} too")
        shutil.copyfile(template, target)
        created.append(name)
    return created


@dataclass
class Settings:
    """All loaded configuration plus the locations it came from."""

    config_dir: Path
    app: AppConfig
    categories: CategoriesConfig
    rules: RulesConfig

    @property
    def root(self) -> Path:
        return self.config_dir.parent

    def resolve(self, path: str) -> Path:
        p = Path(path)
        return p if p.is_absolute() else self.root / p

    @property
    def db_path(self) -> Path:
        return self.resolve(self.app.database.path)

    @property
    def plugins_dir(self) -> Path:
        """User plugins, one folder each."""
        return self.config_dir / "plugins"

    @property
    def import_dir(self) -> Path:
        return self.resolve(self.app.import_folder)

    @property
    def model_path(self) -> Path:
        """The trained classifier; a ``.joblib`` name from configurations of the
        scikit-learn version becomes ``.npz``."""
        path = self.resolve(self.app.ml.model_path)
        return path.with_suffix(".npz") if path.suffix == ".joblib" else path

    @property
    def categories_file(self) -> Path:
        return self.config_dir / "categories.yaml"

    @property
    def rules_file(self) -> Path:
        return self.config_dir / "rules.yaml"

    @property
    def config_file(self) -> Path:
        return self.config_dir / "config.yaml"

    def reload_rules(self) -> None:
        self.rules = _validate(RulesConfig, self.rules_file)

    def reload_categories(self) -> None:
        self.categories = _validate(CategoriesConfig, self.categories_file)


def _validate[T: BaseModel](model: type[T], path: Path) -> T:
    try:
        return model.model_validate(load_yaml(path))
    except Exception as e:  # pydantic ValidationError or YAML errors
        raise ConfigError(f"{path.name}: {e}") from e


def load_settings(config_dir: Path | None = None) -> Settings:
    config_dir = (config_dir or default_config_dir()).resolve()
    ensure_config_files(config_dir)
    settings = Settings(
        config_dir=config_dir,
        app=_validate(AppConfig, config_dir / "config.yaml"),
        categories=_validate(CategoriesConfig, config_dir / "categories.yaml"),
        rules=_validate(RulesConfig, config_dir / "rules.yaml"),
    )
    _check_rule_categories(settings)
    return settings


def _check_rule_categories(settings: Settings) -> None:
    known = set(settings.categories.full_keys())
    for r in settings.rules.rules:
        if r.category not in known:
            raise ConfigError(f"rules.yaml: rule '{r.id}' uses unknown category '{r.category}'")
    for m in settings.app.mortgage:
        for key in (m.interest_category, m.principal_category):
            if key is not None and key not in known:
                raise ConfigError(f"config.yaml: mortgage '{m.name}' uses unknown category '{key}'")
    for a in settings.app.own_accounts:
        if a.transfer_category is not None and a.transfer_category not in known:
            raise ConfigError(
                f"config.yaml: account '{a.name}' uses unknown category '{a.transfer_category}'"
            )
