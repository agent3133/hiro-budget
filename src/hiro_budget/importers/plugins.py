"""Plugins: every way bookings come in is one (issues #24, #82).

A plugin is a folder with a ``plugin.yaml`` manifest (plugin API 2). It provides file
importers (Python classes reading a statement file), CSV formats (``*.csv.yaml`` for the
generic CSV engine, which turns each into a file importer), connectors (Python classes
fetching from a bank, ``importers.connectors``) and enrichers (functions that derive
merchant and purchase date from a bank's booking texts), or is a library that other plugins
require. All of them deliver a ``ParsedStatement``. Built-in plugins live in the package
``hiro_budget.plugins``, user plugins in ``config/plugins/``.

``discover`` reads the manifests and decides which plugins are available:

- a manifest must be valid (unknown keys are errors), name its folder and an API version
  of ``plugin_api``;
- a user plugin with Python code (importers, connectors, enrichers, or any module file) runs
  only when
  the user enabled it (``plugins.enabled`` in config.yaml); plugins with CSV formats only
  need no enabling;
- a user plugin with the name of a built-in one replaces it;
- a plugin whose requirement is missing, unavailable or circular is not available.

Python modules are imported only when the registry is asked for importers, connectors or
enrichers;
plugin code imports other plugins through ``plugin_api.module`` so that a replaced plugin
is used everywhere. Every failure is recorded as a problem of its plugin and skips it.
"""

from __future__ import annotations

import importlib
import importlib.machinery
import logging
import re
import sys
import types
from collections.abc import Callable, Collection
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from hiro_budget.config import (
    CSV_FORMATS_FILE,
    CsvFormat,
    OwnAccount,
    Settings,
    load_yaml,
    save_yaml,
)
from hiro_budget.importers.base import FileImporter
from hiro_budget.importers.connectors import Connector

log = logging.getLogger(__name__)

# Plugin API versions this app supports. 2 (#82): connectors next to file importers; all
# plugins are built in or the user's own, so version 1 was replaced without a transition.
# 3 (#84) only adds: ``frontends`` (commands of the command line, e.g. ``hiro-budget mcp``)
# and ``plugin_api.Operations``; plugins of version 2 keep working.
API_VERSIONS = frozenset({2, 3})
MANIFEST = "plugin.yaml"
BUILTIN_PACKAGE = "hiro_budget.plugins"
USER_PACKAGE = "hiro_budget_user_plugins"  # synthetic package of config/plugins/

Origin = Literal["builtin", "user"]
Enricher = Callable[..., tuple[str | None, object]]


class ImporterEntry(BaseModel):
    """A class of the plugin: a file importer (``importers``) or a connector
    (``connectors``)."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    module: str = Field(pattern=r"^[A-Za-z_]\w*$")  # in the plugin folder
    class_: str = Field(alias="class", pattern=r"^[A-Za-z_]\w*$")


class EnricherEntry(BaseModel):
    """``function`` (``module:function``) enriches bookings of these banks."""

    model_config = ConfigDict(extra="forbid")

    bank_codes: list[str] = []
    institution: str | None = None
    function: str = Field(pattern=r"^[A-Za-z_]\w*:[A-Za-z_]\w*$")


class PluginManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")  # = folder name
    version: str
    api: int
    description: str = ""
    requires: list[str] = []
    importers: list[ImporterEntry] = []  # file importers
    formats: list[str] = []  # *.csv.yaml files in the plugin folder
    connectors: list[ImporterEntry] = []
    enrichers: list[EnricherEntry] = []
    frontends: list[ImporterEntry] = []  # commands of the command line (API 3)

    @field_validator("version", mode="before")
    @classmethod
    def _version_text(cls, v):
        return str(v) if isinstance(v, (int, float)) else v  # "version: 1" in YAML

    @property
    def declares_code(self) -> bool:
        return bool(self.importers or self.connectors or self.enrichers or self.frontends)


Status = Literal["available", "disabled", "replaced", "error"]


@dataclass
class Plugin:
    path: Path
    origin: Origin
    parent_package: str  # the plugin is the subpackage <parent_package>.<name>
    manifest: PluginManifest | None = None
    status: Status = "available"
    problem: str = ""  # why it is not available (status error or disabled)

    @property
    def name(self) -> str:
        return self.path.name  # = the manifest's name of a valid plugin

    @property
    def package(self) -> str:
        return f"{self.parent_package}.{self.name}"

    @property
    def spec(self) -> PluginManifest:
        """The manifest; every plugin that is not an error has one."""
        assert self.manifest is not None
        return self.manifest

    @property
    def has_code(self) -> bool:
        """Declares importers/connectors/enrichers or holds any importable module (``.py``,
        ``.pyc``,
        extension module): another plugin can import those through ``plugin_api.module``,
        so a "library" plugin without a declared importer is code, too (#73)."""
        if self.manifest is not None and self.manifest.declares_code:
            return True
        suffixes = tuple(importlib.machinery.all_suffixes())
        return any(f.is_file() and f.name.endswith(suffixes) for f in self.path.rglob("*"))


class _SafeImporter:
    """An importer whose ``detect`` failing does not stop the import of other files."""

    def __init__(self, inner: FileImporter, registry: PluginRegistry, plugin: Plugin) -> None:
        self.inner, self.registry, self.plugin = inner, registry, plugin
        self.name = inner.name

    def detect(self, path: Path) -> bool:
        try:
            return bool(self.inner.detect(path))
        except Exception as e:  # a plugin's bug
            self.registry.fail(self.plugin, f"detect({path.name}) failed: {e!r}")
            return False

    def parse(self, path: Path):
        return self.inner.parse(path)


@dataclass
class PluginRegistry:
    plugins: list[Plugin] = field(default_factory=list)  # all found, in detection order
    enabled: frozenset[str] = frozenset()  # user plugins whose code may run
    _importers: list[_SafeImporter] | None = None
    _connectors: list[Connector] | None = None
    _enrichers: list[tuple[EnricherEntry, Enricher]] | None = None

    @property
    def available(self) -> list[Plugin]:
        return [p for p in self.plugins if p.status == "available"]

    def get(self, name: str) -> Plugin | None:
        return next((p for p in self.available if p.name == name), None)

    def problems(self) -> list[tuple[str, str]]:
        return [(p.name, p.problem) for p in self.plugins if p.status == "error"]

    def fail(self, plugin: Plugin, message: str) -> None:
        log.warning("plugin %s: %s", plugin.name, message)
        if plugin.status == "available":
            plugin.status, plugin.problem = "error", message

    # -- code ----------------------------------------------------------------------

    def module(self, plugin: str, module: str) -> types.ModuleType:
        """A module of an available plugin (raises LookupError / ImportError)."""
        p = self.get(plugin)
        if p is None:
            raise LookupError(f"plugin '{plugin}' is not available")
        if p.origin == "user" and p.name not in self.enabled:  # never without the user's word
            raise LookupError(f"plugin '{plugin}' is not enabled")
        return importlib.import_module(f"{p.package}.{module}")

    def importers(self) -> list[FileImporter]:
        """Instances of all file importer classes of available plugins, in detection
        order (without the CSV formats: see ``file_importers``)."""
        if self._importers is None:
            self._importers = []
            for p in self.available:
                for entry in p.spec.importers:
                    try:
                        cls = getattr(self.module(p.name, entry.module), entry.class_)
                        self._importers.append(_SafeImporter(cls(), self, p))
                    except Exception as e:
                        self.fail(p, f"importer {entry.module}:{entry.class_}: {e!r}")
            # a plugin that failed half-way contributes nothing
            failed = {p.name for p in self.plugins if p.status == "error"}
            self._importers = [i for i in self._importers if i.plugin.name not in failed]
        found: list[FileImporter] = list(self._importers)
        return found

    def file_importers(self, own_accounts: list[OwnAccount] | None = None) -> list[FileImporter]:
        """Everything that reads a file: the importer classes, then the CSV engine for each
        CSV format (``own_accounts``: what its transfers are recognised by)."""
        found = self.importers()
        if formats := self.formats():
            from hiro_budget.importers.csv_import import CsvImporter

            found += [CsvImporter(f, own_accounts or []) for f in formats]
        return found

    def connectors(self) -> list[Connector]:
        """Instances of all connectors of available plugins (the first of a name wins)."""
        if self._connectors is None:
            self._connectors = []
            seen: set[str] = set()
            for p in self.available:
                for entry in p.spec.connectors:
                    try:
                        connector = getattr(self.module(p.name, entry.module), entry.class_)()
                    except Exception as e:
                        self.fail(p, f"connector {entry.module}:{entry.class_}: {e!r}")
                        continue
                    if connector.name not in seen:
                        seen.add(connector.name)
                        self._connectors.append(connector)
        return list(self._connectors)

    def connector(self, name: str) -> Connector | None:
        return next((c for c in self.connectors() if c.name == name), None)

    def frontends(self, reserved: Collection[str] = ()) -> list[Any]:
        """Instances of all frontend classes of available plugins (API 3): objects with
        ``command``, ``help``, ``add_arguments(parser)`` and ``run(args, operations)``.
        A command that is in ``reserved`` (built in) or taken by an earlier plugin is a
        problem of the later plugin, not a crash."""
        found: list[Any] = []
        taken = set(reserved)
        for p in self.available:
            for entry in p.spec.frontends:
                try:
                    frontend = getattr(self.module(p.name, entry.module), entry.class_)()
                    command = frontend.command
                    if not isinstance(command, str) or not command.isidentifier():
                        raise ValueError(f"command {command!r} is not a name")
                    if command in taken:
                        raise ValueError(f"the command '{command}' exists already")
                except Exception as e:
                    self.fail(p, f"frontend {entry.module}:{entry.class_}: {e!r}")
                    continue
                taken.add(command)
                found.append(frontend)
        return found

    def enricher_for(
        self, *, bank_code: str | None = None, institution: str | None = None
    ) -> Enricher | None:
        """The enricher registered for a bank code (preferred) or an institution name."""
        if self._enrichers is None:
            self._enrichers = []
            for p in self.available:
                for entry in p.spec.enrichers:
                    module, name = entry.function.split(":")
                    try:
                        self._enrichers.append((entry, getattr(self.module(p.name, module), name)))
                    except Exception as e:
                        self.fail(p, f"enricher {entry.function}: {e!r}")
        for entry, func in self._enrichers:
            if bank_code and bank_code in entry.bank_codes:
                return func
        wanted = (institution or "").casefold()
        for entry, func in self._enrichers:
            if wanted and (entry.institution or "").casefold() == wanted:
                return func
        return None

    # -- CSV formats ---------------------------------------------------------------

    def formats(self) -> list[CsvFormat]:
        """CSV formats of available plugins, in detection order."""
        result: list[tuple[Plugin, CsvFormat]] = []
        for p in self.available:
            for name in p.spec.formats:
                try:
                    result.append((p, CsvFormat.model_validate(load_yaml(p.path / name))))
                except Exception as e:
                    self.fail(p, f"format {name}: {_describe(e)}")
        return [f for p, f in result if p.status == "available"]


def _describe(error: Exception) -> str:
    """One line a user can act on: each wrong setting and why (pydantic's own text starts
    with "1 validation error for PluginManifest" and spans lines)."""
    if isinstance(error, ValidationError):
        return "; ".join(
            f"{'.'.join(str(part) for part in err['loc']) or 'file'}: {err['msg']}"
            for err in error.errors()
        )
    return " ".join(str(error).split())


def _read(path: Path, origin: Origin, parent_package: str) -> Plugin:
    plugin = Plugin(path, origin, parent_package)
    try:
        plugin.manifest = PluginManifest.model_validate(load_yaml(path / MANIFEST))
    except Exception as e:  # YAML or validation error
        plugin.status, plugin.problem = "error", f"{MANIFEST}: {_describe(e)}"
        return plugin
    m = plugin.manifest
    if m.name != path.name:
        plugin.status, plugin.problem = "error", f"name '{m.name}' differs from its folder"
    elif m.api not in API_VERSIONS:
        plugin.status, plugin.problem = "error", f"plugin API {m.api} is not supported"
    elif m.frontends and m.api < 3:
        plugin.status, plugin.problem = "error", "frontends need plugin API 3"
    elif missing := [f for f in m.formats if not (path / f).is_file()]:
        plugin.status, plugin.problem = "error", f"missing format files: {', '.join(missing)}"
    return plugin


def _folders(directory: Path | None) -> list[Path]:
    if directory is None or not directory.is_dir():
        return []
    return sorted(
        (d for d in directory.iterdir() if d.is_dir() and (d / MANIFEST).is_file()),
        key=lambda d: d.name,
    )


def _resolve_requirements(plugins: list[Plugin]) -> None:
    """Mark plugins whose requirements are not available (also through other plugins)."""
    by_name = {p.name: p for p in plugins if p.status == "available"}
    state: dict[str, bool] = {}  # name -> usable

    def usable(name: str, chain: tuple[str, ...]) -> bool:
        if name in state:
            return state[name]
        p = by_name.get(name)
        if p is None:
            return False
        if name in chain:
            p.status, p.problem = "error", f"circular requirement: {' -> '.join((*chain, name))}"
            state[name] = False
            return False
        for req in p.spec.requires:
            if not usable(req, (*chain, name)):
                if p.status == "available":
                    p.status, p.problem = "error", f"requires '{req}', which is not available"
                state[name] = False
                return False
        state[name] = p.status == "available"
        return state[name]

    for p in plugins:
        if p.status == "available":
            usable(p.name, ())


def _user_package(directory: Path) -> None:
    """Make ``config/plugins/`` importable as the package ``USER_PACKAGE``."""
    for name in [m for m in sys.modules if m == USER_PACKAGE or m.startswith(USER_PACKAGE + ".")]:
        del sys.modules[name]  # another config directory (tests) or a fresh discovery
    package = types.ModuleType(USER_PACKAGE)
    package.__path__ = [str(directory)]
    sys.modules[USER_PACKAGE] = package
    importlib.invalidate_caches()


def builtin_dir() -> Path:
    import hiro_budget.plugins as builtin

    return Path(builtin.__path__[0])


def discover(
    user_dir: Path | None,
    enabled: Collection[str] = (),
    *,
    builtin: Path | None = None,
    builtin_package: str = BUILTIN_PACKAGE,
) -> PluginRegistry:
    """Find the plugins: user ones first (they replace built-in ones of the same name).
    ``builtin``/``builtin_package``: another folder of built-in plugins (tests)."""
    users = [_read(d, "user", USER_PACKAGE) for d in _folders(user_dir)]
    builtins = [_read(d, "builtin", builtin_package) for d in _folders(builtin or builtin_dir())]
    for p in users:
        if p.status == "available" and p.has_code and p.name not in set(enabled):
            p.status, p.problem = "disabled", "contains code: enable it in the settings"
    replacing = {p.name for p in users if p.status == "available"}
    for p in builtins:
        if p.name in replacing:
            p.status, p.problem = "replaced", "replaced by the user plugin of this name"
    plugins = [*users, *builtins]
    _resolve_requirements(plugins)
    if user_dir is not None:
        _user_package(user_dir)
    registry = PluginRegistry(plugins, frozenset(enabled))
    for name, problem in registry.problems():
        log.warning("plugin %s: %s", name, problem)
    return registry


# -- the registry of the running app ------------------------------------------------

_current: PluginRegistry | None = None
_current_key: tuple | None = None


def registry(user_dir: Path | None = None, enabled: Collection[str] = ()) -> PluginRegistry:
    """The registry for this user plugin folder and enabled list (built once, then reused;
    plugins are read again after ``reset`` or with other settings)."""
    global _current, _current_key
    key = (str(user_dir) if user_dir else None, tuple(sorted(enabled)))
    if _current is None or key != _current_key:
        _current, _current_key = discover(user_dir, enabled), key
    return _current


def of(settings: Settings | None) -> PluginRegistry:
    """The registry for the app's settings (user plugins and which may run); without
    settings the built-in plugins only."""
    if settings is None:
        return registry(None)
    return registry(settings.plugins_dir, settings.app.plugins.enabled)


def current() -> PluginRegistry:
    """The registry last built (an empty one before any import)."""
    return _current if _current is not None else registry(None)


def reset() -> None:
    global _current, _current_key
    _current = _current_key = None


# -- the former csv_formats.yaml --------------------------------------------------------


def _slug(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "format"
    return f"{'csv_' if not slug[0].isalpha() else ''}{slug}_csv"


def migrate_csv_formats(config_dir: Path) -> list[str]:
    """Move the formats of ``config/csv_formats.yaml`` (before plugins) into plugins, once.

    Formats equal to a built-in one are dropped; each other format becomes a user plugin
    ``config/plugins/<name>_csv/`` (no code: needs no enabling). ``ing_card_details: true``
    becomes ``enrich: ING``. The file is renamed to ``csv_formats.yaml.migrated``. Returns
    what happened, for the log; nothing is changed if the file is invalid.
    """
    legacy = config_dir / CSV_FORMATS_FILE
    if not legacy.is_file():
        return []
    try:
        doc = load_yaml(legacy)
        entries = list(doc.get("formats") or [])
        for entry in entries:
            if entry.pop("ing_card_details", False):
                entry["enrich"] = "ING"
        formats = [CsvFormat.model_validate(e) for e in entries]
    except Exception as e:
        message = f"{CSV_FORMATS_FILE} not migrated to plugins, it is invalid: {e}"
        log.error(message)
        return [message]
    builtin = {f.name: f for f in discover(None).formats()}
    messages = []
    user_dir = config_dir / "plugins"
    for entry, fmt in zip(entries, formats, strict=True):
        if builtin.get(fmt.name) == fmt:
            messages.append(f"format '{fmt.name}': built in now, dropped from {CSV_FORMATS_FILE}")
            continue
        name, n = _slug(fmt.name), 2
        while (user_dir / name).exists():
            name, n = f"{_slug(fmt.name)}_{n}", n + 1
        folder = user_dir / name
        folder.mkdir(parents=True)
        _write_format(folder / "format.csv.yaml", entry, fmt)
        lines = [
            f"name: {name}",
            "version: 1.0.0",
            "api: 2",
            f"description: CSV format {fmt.name} (from {CSV_FORMATS_FILE})",
            *(
                [f"requires: [{_ENRICHER_PLUGINS[fmt.enrich]}]"]
                if fmt.enrich in _ENRICHER_PLUGINS
                else []
            ),
            "formats: [format.csv.yaml]",
        ]
        (folder / MANIFEST).write_text("\n".join(lines) + "\n", encoding="utf-8")
        messages.append(f"format '{fmt.name}': now the user plugin {name}")
    target = legacy.with_name(CSV_FORMATS_FILE + ".migrated")
    n = 2
    while target.exists():
        target, n = legacy.with_name(f"{CSV_FORMATS_FILE}.migrated{n}"), n + 1
    legacy.rename(target)
    messages.append(f"{CSV_FORMATS_FILE} renamed to {target.name}")
    for m in messages:
        log.info(m)
    return messages


def _plain(node):
    """ruamel's commented nodes as plain dicts and lists (no comments)."""
    if isinstance(node, dict):
        return {k: _plain(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_plain(v) for v in node]
    return node


def _write_format(path: Path, entry, fmt: CsvFormat) -> None:
    """Write a format keeping the user's comments; ruamel can write broken YAML when a
    key with a comment was removed: then without comments."""
    save_yaml(path, entry)
    try:
        if CsvFormat.model_validate(load_yaml(path)) == fmt:
            return
    except Exception:
        pass
    save_yaml(path, _plain(entry))


# built-in library plugins providing the enricher a migrated format names
_ENRICHER_PLUGINS = {"ING": "ing_common"}
