"""Editing the YAML configuration from the GUI.

Files are loaded in ruamel round-trip mode and only the edited section is replaced,
so comments at the top of the file and in other sections are preserved.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ruamel.yaml.comments import CommentedMap, CommentedSeq
from sqlalchemy.orm import Session

from hiro_budget.config import (
    AppConfig,
    CategoriesConfig,
    ConfigError,
    FintsConfig,
    OwnAccount,
    RuleDef,
    RulesConfig,
    Settings,
    TargetDef,
    load_yaml,
    save_yaml,
)
from hiro_budget.services.categorization.pipeline import load_transactions
from hiro_budget.services.categorization.rules import CompiledRule


def rule_match_count(session: Session, rule: RuleDef) -> tuple[int, int]:
    """How many bookings the rule matches: (total, currently uncategorized)."""
    compiled = CompiledRule.compile(rule)
    total = uncategorized = 0
    for tx in load_transactions(session):
        if compiled.matches(tx):
            total += 1
            if any(s.is_uncategorized for s in tx.splits):
                uncategorized += 1
    return total, uncategorized


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
    data = {"type": target.type, "amount": target.amount}
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
    for login in (doc.get("fints") or {}).get("logins") or []:
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


def save_fints(settings: Settings, fints: FintsConfig) -> None:
    """Write the bank logins (``fints``) to config.yaml. PINs are never part of them."""
    doc = load_yaml(settings.config_file)
    section = CommentedMap()
    if fints.product_id:
        section["product_id"] = fints.product_id
    logins = CommentedSeq()
    for login in fints.logins:
        m = CommentedMap(
            name=login.name, bank_code=login.bank_code, server=login.server, login_id=login.login_id
        )
        accounts = CommentedSeq()
        for link in login.accounts:
            a = {"bank_id": link.bank_id, "product": link.product, "kind": link.kind}
            a["account"] = link.account
            if link.fetch_from is not None:
                a["fetch_from"] = link.fetch_from
            accounts.append(_flow_map(a))
        m["accounts"] = accounts
        logins.append(m)
    section["logins"] = logins
    doc["fints"] = section
    try:
        new_app = AppConfig.model_validate(doc)
    except Exception as e:
        raise ConfigError(str(e)) from e
    save_yaml(settings.config_file, doc)
    settings.app = new_app


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
