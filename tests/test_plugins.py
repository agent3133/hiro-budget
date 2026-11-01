"""Plugins: discovery, trust, requirements, failures, hooks (issue #24)."""

from __future__ import annotations

import textwrap
import uuid
from datetime import date
from pathlib import Path

import pytest

from hiro_budget.importers import plugins
from hiro_budget.importers.base import find_importer

# A minimal importer: files ending in .demo, one booking per line "YYYY-MM-DD;cents;payee".
IMPORTER = """
from datetime import date
from pathlib import Path

from hiro_budget.plugin_api import ImportError_, ParsedStatement, ParsedTransaction


class DemoImporter:
    name = "{name}"

    def detect(self, path: Path) -> bool:
        return path.suffix == ".demo"

    def parse(self, path: Path) -> ParsedStatement:
        rows = [line.split(";") for line in path.read_text().splitlines() if line]
        if not rows:
            raise ImportError_("empty")
        return ParsedStatement(
            importer=self.name,
            institution="Demo Bank",
            account_name="Demo",
            account_kind="giro",
            account_iban=None,
            transactions=[
                ParsedTransaction(date.fromisoformat(d), int(c), "Demo", counterparty=p)
                for d, c, p in rows
            ],
        )
"""


def write_plugin(folder: Path, name: str, manifest: str, files: dict[str, str] | None = None):
    d = folder / name
    d.mkdir(parents=True)
    (d / "plugin.yaml").write_text(textwrap.dedent(manifest), encoding="utf-8")
    for file, text in (files or {}).items():
        (d / file).write_text(textwrap.dedent(text), encoding="utf-8")
    return d


def importer_plugin(folder: Path, name: str, importer_name: str = "demo", requires=()) -> Path:
    req = f"requires: [{', '.join(requires)}]\n" if requires else ""
    return write_plugin(
        folder,
        name,
        f"name: {name}\nversion: 1.0.0\napi: 1\n{req}"
        "importers:\n  - {module: importer, class: DemoImporter}\n",
        {"importer.py": IMPORTER.format(name=importer_name)},
    )


@pytest.fixture(autouse=True)
def fresh_registry():
    plugins.reset()
    yield
    plugins.reset()


@pytest.fixture
def builtin(tmp_path, monkeypatch):
    """An empty folder of built-in plugins, importable as its own package."""
    package = f"builtin_{uuid.uuid4().hex[:8]}"
    folder = tmp_path / package
    folder.mkdir()
    (folder / "__init__.py").write_text("", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    return folder, package


def discover(user: Path | None, builtin, enabled=()) -> plugins.PluginRegistry:
    folder, package = builtin
    reg = plugins.discover(user, enabled, builtin=folder, builtin_package=package)
    plugins._current = reg  # as plugins.registry() does, for plugin_api.module
    return reg


def status(reg: plugins.PluginRegistry) -> dict[str, tuple[str, str]]:
    return {p.name: (p.origin, p.status) for p in reg.plugins}


def statuses(reg: plugins.PluginRegistry) -> list[tuple[str, str, str]]:
    return [(p.name, p.origin, p.status) for p in reg.plugins]


def test_the_package_has_a_builtin_plugin_folder():
    assert plugins.builtin_dir().is_dir()


def test_builtin_importer_is_found_and_detects(builtin, tmp_path):
    importer_plugin(builtin[0], "demo_txt", "demo_txt")
    reg = discover(None, builtin)
    assert status(reg) == {"demo_txt": ("builtin", "available")}
    demo = tmp_path / "x.demo"
    demo.write_text("2026-03-01;-500;Shop\n", encoding="utf-8")
    importer = find_importer(demo, reg.importers())
    assert importer.name == "demo_txt"
    assert [t.amount_cents for t in importer.parse(demo).transactions] == [-500]


def test_a_broken_manifest_skips_only_its_plugin(builtin):
    folder = builtin[0]
    importer_plugin(folder, "good")
    write_plugin(folder, "typo", "name: typo\nversion: '1'\napi: 1\nimporter: []\n")
    write_plugin(folder, "renamed", "name: other\nversion: '1'\napi: 1\n")
    write_plugin(folder, "future", "name: future\nversion: '1'\napi: 2\n")
    write_plugin(
        folder, "noformat", "name: noformat\nversion: '1'\napi: 1\nformats: [x.csv.yaml]\n"
    )
    write_plugin(folder, "badyaml", "name: [unclosed\n")
    reg = discover(None, builtin)
    assert [p.name for p in reg.available] == ["good"]
    problems = dict(reg.problems())
    assert "Extra inputs are not permitted" in problems["typo"]
    assert "differs from its folder" in problems["renamed"]
    assert "plugin API 2 is not supported" in problems["future"]
    assert "missing format files: x.csv.yaml" in problems["noformat"]
    assert "plugin.yaml" in problems["badyaml"]


def test_a_numeric_version_is_text(builtin):
    write_plugin(builtin[0], "lib", "name: lib\nversion: 1\napi: 1\n")
    assert discover(None, builtin).get("lib").manifest.version == "1"


def test_requirements(builtin):
    folder = builtin[0]
    write_plugin(
        folder,
        "lib",
        "name: lib\nversion: '1'\napi: 1\n",
        {"helpers.py": "def greet():\n    return 'from lib'\n"},
    )
    importer_plugin(folder, "uses_lib", requires=["lib"])
    importer_plugin(folder, "needs_missing", requires=["missing"])
    importer_plugin(folder, "needs_needs_missing", requires=["needs_missing"])
    importer_plugin(folder, "cycle_a", requires=["cycle_b"])
    importer_plugin(folder, "cycle_b", requires=["cycle_a"])
    reg = discover(None, builtin)
    assert sorted(p.name for p in reg.available) == ["lib", "uses_lib"]
    problems = dict(reg.problems())
    assert "requires 'missing'" in problems["needs_missing"]
    assert "requires 'needs_missing'" in problems["needs_needs_missing"]
    assert "circular requirement" in problems["cycle_a"] + problems["cycle_b"]
    from hiro_budget import plugin_api

    assert plugin_api.module("lib", "helpers").greet() == "from lib"
    with pytest.raises(LookupError):
        plugin_api.module("needs_missing", "importer")


def test_user_plugins_with_code_need_enabling(builtin, tmp_path):
    user = tmp_path / "config_plugins"
    importer_plugin(user, "mine")
    write_plugin(
        user,
        "my_format",
        "name: my_format\nversion: '1'\napi: 1\nformats: [bank.csv.yaml]\n",
        {
            "bank.csv.yaml": """\
                name: My Bank
                detect: {header_has: [Datum, Betrag]}
                account: {name: My Bank}
                columns: {booking_date: Datum, amount: Betrag}
            """
        },
    )
    reg = discover(user, builtin)
    assert status(reg) == {"mine": ("user", "disabled"), "my_format": ("user", "available")}
    assert reg.importers() == []
    assert [f.name for f in reg.formats()] == ["My Bank"]  # no code: no enabling needed
    reg = discover(user, builtin, enabled=["mine"])
    assert [i.name for i in reg.importers()] == ["demo"]


def test_user_plugin_replaces_the_builtin_one_everywhere(builtin, tmp_path):
    user = tmp_path / "config_plugins"
    for folder, text in ((builtin[0], "built-in"), (user, "user")):
        write_plugin(
            folder,
            "lib",
            "name: lib\nversion: '1'\napi: 1\n",
            {"helpers.py": f"def greet():\n    return '{text}'\n"},
        )
    importer_plugin(builtin[0], "uses_lib", requires=["lib"])
    reg = discover(user, builtin)
    assert statuses(reg) == [
        ("lib", "user", "available"),
        ("lib", "builtin", "replaced"),
        ("uses_lib", "builtin", "available"),
    ]
    from hiro_budget import plugin_api

    assert plugin_api.module("lib", "helpers").greet() == "user"


def test_failing_plugin_code_is_reported_and_skipped(builtin, tmp_path):
    folder = builtin[0]
    importer_plugin(folder, "good", "good")
    write_plugin(
        folder,
        "broken_import",
        "name: broken_import\nversion: '1'\napi: 1\n"
        "importers:\n  - {module: importer, class: Missing}\n",
        {"importer.py": "import does_not_exist\n"},
    )
    write_plugin(
        folder,
        "broken_detect",
        "name: broken_detect\nversion: '1'\napi: 1\n"
        "importers:\n  - {module: importer, class: Bad}\n",
        {
            "importer.py": """\
                class Bad:
                    name = "bad"

                    def detect(self, path):
                        raise RuntimeError("bug")

                    def parse(self, path):
                        raise AssertionError
            """
        },
    )
    reg = discover(None, builtin)
    names = [i.name for i in reg.importers()]
    assert names == ["bad", "good"]
    assert "No module named 'does_not_exist'" in dict(reg.problems())["broken_import"]
    demo = tmp_path / "x.demo"
    demo.write_text("2026-03-01;-500;Shop\n", encoding="utf-8")
    assert find_importer(demo, reg.importers()).name == "good"
    assert "RuntimeError('bug')" in dict(reg.problems())["broken_detect"]


def test_enricher_by_bank_code_or_institution(builtin):
    write_plugin(
        builtin[0],
        "bank_common",
        """\
        name: bank_common
        version: '1'
        api: 1
        enrichers:
          - {bank_codes: ['12345678'], institution: Demo Bank, function: texts:enrich}
        """,
        {
            "texts.py": "def enrich(counterparty, purpose, booking_date):\n"
            "    return counterparty.upper(), booking_date\n"
        },
    )
    reg = discover(None, builtin)
    by_code = reg.enricher_for(bank_code="12345678")
    assert by_code("shop", "", date(2026, 3, 1)) == ("SHOP", date(2026, 3, 1))
    assert reg.enricher_for(institution="demo bank") is by_code
    assert reg.enricher_for(bank_code="99999999") is None


def test_import_through_an_enabled_user_plugin(app, tmp_path):
    from hiro_budget.services import import_service

    settings = app.settings
    importer_plugin(settings.plugins_dir, "demo_bank", "demo_bank")
    settings.app.plugins.enabled = ["demo_bank"]
    demo = tmp_path / "statement.demo"
    demo.write_text("2026-03-01;-500;Shop\n2026-03-02;1000;Employer\n", encoding="utf-8")
    with app.session() as s:
        r = import_service.import_file(s, settings, demo)
    assert r.added == 2
    settings.app.plugins.enabled = []  # disabled again: the file is not recognised any more
    with app.session() as s, pytest.raises(Exception, match="no importer recognizes"):
        import_service.preview(s, demo, settings=settings)


# -- the former csv_formats.yaml ----------------------------------------------------------


def legacy_formats(config_dir: Path, extra: list[dict]) -> Path:
    """A csv_formats.yaml as before plugins: the built-in formats (ING still with
    ing_card_details) plus ``extra``."""
    from hiro_budget.config import load_yaml, save_yaml

    entries = []
    for name in ("ing_umsatzanzeige_csv", "revolut_csv", "paypal_csv"):
        entry = load_yaml(plugins.builtin_dir() / name / "format.csv.yaml")
        if entry.pop("enrich", None) == "ING":
            entry["ing_card_details"] = True
        entries.append(entry)
    path = config_dir / "csv_formats.yaml"
    save_yaml(path, {"formats": [*entries, *extra]})
    return path


MY_BANK = {
    "name": "My Bank",
    "detect": {"header_has": ["Buchungstag", "Umsatz"]},
    "delimiter": ";",
    "account": {"name": "My Bank"},
    "columns": {"booking_date": "Buchungstag", "amount": "Umsatz"},
}


def test_csv_formats_yaml_becomes_plugins(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    legacy = legacy_formats(config_dir, [MY_BANK])
    messages = plugins.migrate_csv_formats(config_dir)
    assert not legacy.exists() and (config_dir / "csv_formats.yaml.migrated").is_file()
    assert sum("built in now" in m for m in messages) == 3
    assert "format 'My Bank': now the user plugin my_bank_csv" in messages
    reg = plugins.discover(config_dir / "plugins")
    assert ("my_bank_csv", "user", "available") in [
        (p.name, p.origin, p.status) for p in reg.plugins
    ]
    assert sorted(f.name for f in reg.formats()) == [
        "ING Umsatzanzeige",
        "My Bank",
        "PayPal",
        "Revolut",
    ]
    assert plugins.migrate_csv_formats(config_dir) == []  # once


def test_a_changed_builtin_format_replaces_the_builtin_plugin(tmp_path):
    from hiro_budget.config import load_yaml

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    legacy = legacy_formats(config_dir, [])
    doc = load_yaml(legacy)
    doc["formats"][1]["institution"] = "Revolut Bank"  # the user's Revolut format differs
    from hiro_budget.config import save_yaml

    save_yaml(legacy, doc)
    plugins.migrate_csv_formats(config_dir)
    reg = plugins.discover(config_dir / "plugins")
    assert [(p.name, p.origin) for p in reg.available if p.name == "revolut_csv"] == [
        ("revolut_csv", "user")
    ]
    assert next(f for f in reg.formats() if f.name == "Revolut").institution == "Revolut Bank"


def test_migration_keeps_formats_readable_when_comments_move(tmp_path):
    """ing_card_details with a comment: removing it must not break the written YAML."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "csv_formats.yaml").write_text(
        "formats:\n"
        "  - name: My ING\n"
        "    detect: {header_has: [Buchung, Betrag]}\n"
        "    account: {name: Giro}\n"
        "    columns: {booking_date: Buchung, amount: Betrag}\n"
        "    # card texts as on the statements\n"
        "    ing_card_details: true\n"
        "    match_by: date_amount  # PDFs too\n",
        encoding="utf-8",
    )
    plugins.migrate_csv_formats(config_dir)
    reg = plugins.discover(config_dir / "plugins")
    mine = next(f for f in reg.formats() if f.name == "My ING")
    assert (mine.enrich, mine.match_by) == ("ING", "date_amount")
    assert reg.get("my_ing_csv").manifest.requires == ["ing_common"]


def test_an_invalid_csv_formats_yaml_is_left_alone(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    legacy = config_dir / "csv_formats.yaml"
    legacy.write_text("formats:\n  - name: Broken\n    delimeter: ';'\n", encoding="utf-8")
    messages = plugins.migrate_csv_formats(config_dir)
    assert legacy.exists() and "not migrated" in messages[0]
    assert not (config_dir / "plugins").exists()


def test_the_app_migrates_at_start(config_dir):
    from hiro_budget.app import App

    legacy_formats(config_dir, [MY_BANK])
    App.from_config_dir(config_dir)
    assert (config_dir / "csv_formats.yaml.migrated").is_file()
    assert (config_dir / "plugins" / "my_bank_csv" / "plugin.yaml").is_file()
