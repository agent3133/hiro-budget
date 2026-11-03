"""Editing the YAML configuration from the GUI.

Files are loaded in ruamel round-trip mode and only the edited section is replaced,
so comments at the top of the file and in other sections are preserved.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ruamel.yaml.comments import CommentedMap, CommentedSeq
from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from hiro_budget.config import (
    AppConfig,
    BankAccessConfig,
    CategoriesConfig,
    ConfigError,
    OwnAccount,
    RuleDef,
    RulesConfig,
    Settings,
    TargetDef,
    load_yaml,
    save_yaml,
)
from hiro_budget.db.models import Account, Split, Transaction
from hiro_budget.services.categorization.rules import TEXT_FIELDS, CompiledRule


@dataclass(frozen=True)
class _AccountName:
    name: str


@dataclass(frozen=True)
class RuleSample:
    """What a rule looks at in a booking, without the ORM (kept while the rules page is
    open: counting all rules again after an edit only matches, it does not load)."""

    id: int
    counterparty: str | None
    merchant: str | None
    purpose: str | None
    booking_type: str | None
    details: str | None
    amount_cents: int
    account: _AccountName | None
    uncategorized: bool


@dataclass
class RuleHits:
    """Bookings a rule matches, and how many of them it categorizes: the first matching
    rule wins, so a rule below another one can match bookings and still get none."""

    matching: int = 0
    effective: int = 0
    uncategorized: int = 0  # of the matching ones, currently without a category
    ids: list[int] = field(default_factory=list)  # the matching bookings

    @property
    def taken(self) -> int:
        """Matching bookings an earlier rule takes."""
        return self.matching - self.effective


def rule_samples(session: Session) -> list[RuleSample]:
    """All bookings as rules see them, oldest first: plain columns, the uncategorized flag
    as EXISTS in SQL (it was one lazy load of ``split.category`` per booking, #57)."""
    names = {a.id: _AccountName(a.name) for a in session.execute(select(Account.id, Account.name))}
    uncategorized = exists().where(
        Split.transaction_id == Transaction.id,
        Split.category_id.is_(None),
        Split.transfer_account_id.is_(None),
    )
    rows = session.execute(
        select(
            Transaction.id,
            Transaction.counterparty,
            Transaction.merchant,
            Transaction.purpose,
            Transaction.booking_type,
            Transaction.details,
            Transaction.amount_cents,
            Transaction.account_id,
            uncategorized,
        ).order_by(Transaction.booking_date, Transaction.id)
    )
    return [RuleSample(*r[:7], names.get(r[7]), bool(r[8])) for r in rows]


def rule_hits(samples: list[RuleSample], rules: list[RuleDef | None]) -> list[RuleHits]:
    """Hits per rule, in the given order (None, e.g. an invalid draft: no hits).

    Rule by rule over columns: each pattern only sees the bookings the rule's earlier
    conditions left, and each distinct text once (most counterparties and booking types
    repeat): the same hits as matching every booking against every rule, in a fraction of
    the ``re.search`` calls (#57)."""
    compiled = [CompiledRule.compile(r) if r is not None else None for r in rules]
    columns = {name: [getattr(s, name) or "" for s in samples] for name in TEXT_FIELDS}
    columns["account"] = [s.account.name if s.account else "" for s in samples]
    amounts = [s.amount_cents for s in samples]
    taken = [False] * len(samples)
    hits = []
    for rule in compiled:
        h = RuleHits()
        hits.append(h)
        if rule is None:
            continue
        lo, hi = rule.amount_min, rule.amount_max
        rows = [
            i for i, a in enumerate(amounts) if (lo is None or a >= lo) and (hi is None or a <= hi)
        ]
        for name, rx in rule.patterns.items():
            column, seen = columns[name], dict[str, bool]()
            kept = []
            for i in rows:
                value = column[i]
                found = seen.get(value)
                if found is None:
                    found = seen[value] = rx.search(value) is not None
                if found:
                    kept.append(i)
            rows = kept
        h.matching = len(rows)
        for i in rows:
            h.ids.append(samples[i].id)
            h.uncategorized += samples[i].uncategorized
            if not taken[i]:  # the first matching rule categorizes it
                taken[i] = True
                h.effective += 1
    return hits


def rule_match_count(session: Session, rule: RuleDef) -> tuple[int, int]:
    """How many bookings the rule matches: (total, currently uncategorized)."""
    (h,) = rule_hits(rule_samples(session), [rule])
    return h.matching, h.uncategorized


def _flow_map(data: dict) -> CommentedMap:
    m = CommentedMap(data)
    m.fa.set_flow_style()
    return m


def save_rules(settings: Settings, rules: list[RuleDef]) -> None:
    cfg = RulesConfig(rules=rules)  # validates ids
    known = set(settings.categories.full_keys())
    for r in cfg.rules:
        if r.category not in known:
            raise ConfigError(f"rule '{r.id}' uses unknown category '{r.category}'")
    doc = load_yaml(settings.rules_file)
    seq = CommentedSeq()
    for r in cfg.rules:
        match = {k: v for k, v in r.match.model_dump().items() if v is not None}
        seq.append(CommentedMap(id=r.id, category=r.category, match=_flow_map(match)))
    doc["rules"] = seq
    save_yaml(settings.rules_file, doc)
    settings.reload_rules()


def _target_map(target: TargetDef) -> CommentedMap:
    data: dict[str, Any] = {"type": target.type, "amount": target.amount}
    if target.date is not None:
        data["date"] = target.date
    if target.type == "repeating":
        data["every_months"] = target.every_months
    return _flow_map(data)


def _category_map(c) -> CommentedMap:
    m = CommentedMap(key=c.key, name=c.name)
    if c.one_off:
        m["one_off"] = True
    if c.report_shift_days:
        m["report_shift_days"] = c.report_shift_days
    if c.target is not None:
        m["target"] = _target_map(c.target)
    else:
        m.fa.set_flow_style()
    return m


KEY_FORMAT = re.compile(r"^[a-z0-9_]+$")


@dataclass
class KeyRenames:
    """Changed keys of existing groups and categories (old -> new)."""

    groups: dict[str, str] = field(default_factory=dict)
    # full keys ("group.category"), including those changed by a group rename
    categories: dict[str, str] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return bool(self.groups or self.categories)


def save_categories(
    settings: Settings, categories: CategoriesConfig, renames: KeyRenames | None = None
) -> None:
    """Write categories.yaml; ``renames`` also updates rules and config.yaml references."""
    renames = renames or KeyRenames()
    for g in categories.groups:
        for key in (g.key, *(c.key for c in g.categories)):
            if not KEY_FORMAT.match(key):
                raise ConfigError(
                    f"invalid key '{key}': use lower-case letters, digits and underscores"
                )
    keys = set(categories.full_keys())

    def renamed(key: str | None) -> str | None:
        return renames.categories.get(key, key) if key is not None else None

    rules_doc = load_yaml(settings.rules_file)
    for r in rules_doc.get("rules") or []:
        if r.get("category") in renames.categories:
            r["category"] = renames.categories[r["category"]]
    app_doc = load_yaml(settings.config_file)
    for m in app_doc.get("mortgage") or []:
        for name in ("interest_category", "principal_category"):
            if m.get(name) in renames.categories:
                m[name] = renames.categories[m[name]]
    for a in app_doc.get("own_accounts") or []:
        if a.get("transfer_category") in renames.categories:
            a["transfer_category"] = renames.categories[a["transfer_category"]]

    used = [(f"rule '{r.id}'", renamed(r.category)) for r in settings.rules.rules]
    used += [
        (f"mortgage '{m.name}'", renamed(key))
        for m in settings.app.mortgage
        for key in (m.interest_category, m.principal_category)
        if key is not None
    ]
    used += [
        (f"account '{a.name}'", renamed(a.transfer_category))
        for a in settings.app.own_accounts
        if a.transfer_category is not None
    ]
    missing = [f"{who} uses '{key}'" for who, key in used if key not in keys]
    if missing:
        raise ConfigError("categories still in use: " + "; ".join(missing))
    doc = load_yaml(settings.categories_file)
    groups = CommentedSeq()
    for g in categories.groups:
        gm = CommentedMap(key=g.key, name=g.name)
        if g.income:
            gm["income"] = True
        if g.budget_by_group:
            gm["budget"] = "group"
        if g.target is not None:
            gm["target"] = _target_map(g.target)
        gm["categories"] = CommentedSeq(_category_map(c) for c in g.categories)
        groups.append(gm)
    doc["groups"] = groups
    save_yaml(settings.categories_file, doc)
    settings.reload_categories()
    if renames.categories:
        new_app = AppConfig.model_validate(app_doc)
        save_yaml(settings.rules_file, rules_doc)
        save_yaml(settings.config_file, app_doc)
        settings.reload_rules()
        settings.app = new_app


def save_language(settings: Settings, language: str) -> None:
    doc = load_yaml(settings.config_file)
    doc["language"] = language
    save_yaml(settings.config_file, doc)
    settings.app.language = language  # type: ignore[assignment]


def save_theme(settings: Settings, theme: str) -> None:
    doc = load_yaml(settings.config_file)
    doc["theme"] = theme
    save_yaml(settings.config_file, doc)
    settings.app.theme = theme  # type: ignore[assignment]


def save_accounts(
    settings: Settings, accounts: list[OwnAccount], renames: dict[str, str] | None = None
) -> None:
    """Write own_accounts to config.yaml; ``renames`` (old -> new) updates mortgage references."""
    renames = renames or {}
    known = set(settings.categories.full_keys())
    for a in accounts:
        if a.transfer_category and a.transfer_category not in known:
            raise ConfigError(f"account '{a.name}' uses unknown category '{a.transfer_category}'")
    doc = load_yaml(settings.config_file)
    mortgages = list(doc.get("mortgage") or [])
    for m in mortgages:
        if m.get("loan_account") in renames:
            m["loan_account"] = renames[m["loan_account"]]
    for login in (doc.get("bank_access") or {}).get("logins") or []:
        for link in login.get("accounts") or []:
            if link.get("account") in renames:
                link["account"] = renames[link["account"]]
    seq = CommentedSeq()
    for a in accounts:
        m = CommentedMap(name=a.name, kind=a.kind)
        if a.institution:
            m["institution"] = a.institution
        if a.iban:
            m["iban"] = a.iban
        if a.email:
            m["email"] = a.email
        if a.match and (a.match.counterparty or a.match.purpose):
            m["match"] = _flow_map({k: v for k, v in a.match.model_dump().items() if v is not None})
        if a.opening_balance is not None:
            m["opening_balance"] = a.opening_balance
            m["opening_date"] = a.opening_date
        if a.on_budget is not None:
            m["on_budget"] = a.on_budget
        if a.transfer_category:
            m["transfer_category"] = a.transfer_category
        if not a.stale_hint:
            m["stale_hint"] = False
        seq.append(m)
    doc["own_accounts"] = seq
    # Validate the complete result (e.g. mortgage loan accounts must still exist).
    try:
        new_app = AppConfig.model_validate(doc)
    except Exception as e:
        raise ConfigError(str(e)) from e
    save_yaml(settings.config_file, doc)
    settings.app = new_app


def save_plugins(settings: Settings, enabled: list[str]) -> None:
    """Write which user plugins with code may run (``plugins.enabled`` in config.yaml)."""
    doc = load_yaml(settings.config_file)
    section = doc.get("plugins") or CommentedMap()
    section["enabled"] = CommentedSeq(sorted(set(enabled)))
    doc["plugins"] = section
    try:
        new_app = AppConfig.model_validate(doc)
    except Exception as e:
        raise ConfigError(str(e)) from e
    save_yaml(settings.config_file, doc)
    settings.app = new_app


def save_data_folder(settings: Settings, folder: Path | None) -> None:
    """Write ``data_folder`` (#29); None removes it (the folder of database.path again).
    Absolute ``database.path`` / ``ml.model_path`` become plain file names in the folder."""
    doc = load_yaml(settings.config_file)
    if folder is None:
        doc.pop("data_folder", None)
    else:
        if "data_folder" not in doc:  # new key near the top, after the comment block
            doc.insert(0, "data_folder", folder.as_posix())
        else:
            doc["data_folder"] = folder.as_posix()
        for section, key, path in (
            ("database", "path", settings.db_path),
            ("ml", "model_path", settings.model_path),
        ):
            sub = doc.get(section)
            if sub is not None and key in sub and Path(str(sub[key])).is_absolute():
                sub[key] = path.name
    try:
        new_app = AppConfig.model_validate(doc)
    except Exception as e:
        raise ConfigError(str(e)) from e
    save_yaml(settings.config_file, doc)
    settings.app = new_app


def _bank_access_section(access: BankAccessConfig) -> CommentedMap:
    section = CommentedMap()
    connectors = CommentedMap()
    for name, options in access.connectors.items():
        if values := {k: v for k, v in options.items() if v not in (None, "")}:
            connectors[name] = _flow_map(values)
    if connectors:
        section["connectors"] = connectors
    logins = CommentedSeq()
    for login in access.logins:
        m = CommentedMap(connector=login.connector, name=login.name)
        m["settings"] = _flow_map(dict(login.settings))
        accounts = CommentedSeq()
        for link in login.accounts:
            a: dict[str, Any] = {
                "bank_id": link.bank_id,
                "product": link.product,
                "kind": link.kind,
            }
            a["account"] = link.account
            if link.fetch_from is not None:
                a["fetch_from"] = link.fetch_from
            accounts.append(_flow_map(a))
        m["accounts"] = accounts
        logins.append(m)
    section["logins"] = logins
    return section


def save_bank_access(settings: Settings, access: BankAccessConfig) -> None:
    """Write the bank logins (``bank_access``) to config.yaml. Secrets (PINs) are never
    part of them."""
    doc = load_yaml(settings.config_file)
    doc.pop("fints", None)  # the section up to 2026-11 (#82)
    doc["bank_access"] = _bank_access_section(access)
    try:
        new_app = AppConfig.model_validate(doc)
    except Exception as e:
        raise ConfigError(str(e)) from e
    save_yaml(settings.config_file, doc)
    settings.app = new_app


def migrate_bank_access(config_file: Path) -> bool:
    """Rewrite a ``fints`` section (up to 2026-11) as ``bank_access`` (#82), in place of
    it; True if the file changed."""
    doc = load_yaml(config_file)
    if "fints" not in doc:
        return False
    access = AppConfig.model_validate(doc).bank_access  # the validator converts it
    keys = list(doc)
    position = keys.index("fints")
    doc.pop("fints")
    if "bank_access" not in doc:
        doc.insert(position, "bank_access", _bank_access_section(access))
    save_yaml(config_file, doc)
    return True


def save_target(settings: Settings, category_key: str, target: TargetDef | None) -> None:
    """Set or remove the budget target of a category in categories.yaml."""
    group_key, _, cat_key = category_key.partition(".")
    doc = load_yaml(settings.categories_file)
    for g in doc.get("groups") or []:
        if g.get("key") != group_key:
            continue
        for c in g.get("categories") or []:
            if c.get("key") != cat_key:
                continue
            if target is None:
                c.pop("target", None)
            else:
                if hasattr(c, "fa"):
                    c.fa.set_block_style()  # a flow map cannot hold a nested map nicely
                c["target"] = _target_map(target)
            save_yaml(settings.categories_file, doc)
            settings.reload_categories()
            return
    raise ConfigError(f"category '{category_key}' not found in categories.yaml")


def _group_doc(doc, group_key: str):
    for g in doc.get("groups") or []:
        if g.get("key") == group_key:
            return g
    raise ConfigError(f"group '{group_key}' not found in categories.yaml")


def save_group_budget(settings: Settings, group_key: str, by_group: bool) -> None:
    """Budget a group as a whole (one envelope) or per category again.

    Switching back to per category removes the group's target. Call ``App.sync_config``
    afterwards so the budget assignments are converted.
    """
    doc = load_yaml(settings.categories_file)
    g = _group_doc(doc, group_key)
    if by_group:
        if g.get("income"):
            raise ConfigError(f"group '{group_key}': income groups cannot be budgeted")
        # Insert after name/income so the key stays near the top of the group.
        pos = list(g.keys()).index("categories") if "categories" in g else len(g)
        g.pop("budget", None)
        g.insert(pos, "budget", "group")
    else:
        g.pop("budget", None)
        g.pop("target", None)
    save_yaml(settings.categories_file, doc)
    settings.reload_categories()


def save_group_target(settings: Settings, group_key: str, target: TargetDef | None) -> None:
    """Set or remove the budget target of a group budgeted as a whole."""
    doc = load_yaml(settings.categories_file)
    g = _group_doc(doc, group_key)
    if g.get("budget") != "group":
        raise ConfigError(f"group '{group_key}' is not budgeted as a whole")
    if target is None:
        g.pop("target", None)
    else:
        pos = list(g.keys()).index("categories") if "categories" in g else len(g)
        g.pop("target", None)
        g.insert(pos, "target", _target_map(target))
    save_yaml(settings.categories_file, doc)
    settings.reload_categories()
