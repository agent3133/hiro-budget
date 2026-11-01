"""The worked example of docs/plugins.llm.md works: its importer parses its sample, and as a
user plugin (with the doc's manifest) it imports the sample (#24)."""

from __future__ import annotations

import re

import pytest

from hiro_budget.importers import plugins
from tests.conftest import REPO_ROOT, write_statement_pdf

DOC = (REPO_ROOT / "docs" / "plugins.llm.md").read_text(encoding="utf-8")


def block(section: str, language: str, n: int = 0) -> str:
    part = DOC.split(section)[1].split("\n## ")[0]
    return re.findall(rf"```{language}\n(.*?)```", part, re.DOTALL)[n]


def sample_pages() -> list[str]:
    text = block("## 5. Worked example", "text")
    return [p.strip("\n") for p in re.split(r"^===== page \d+ =====$", text, flags=re.M)[1:]]


@pytest.fixture(autouse=True)
def fresh_registry():
    plugins.reset()
    yield
    plugins.reset()


def test_example_importer_parses_the_sample():
    namespace: dict = {}
    exec(compile(block("## 5. Worked example", "python"), "importer.py", "exec"), namespace)
    stmt = namespace["parse_pages"](sample_pages())
    assert stmt.account_iban == "DE11222233334444555566"
    assert stmt.balance_ok and stmt.warnings == []
    assert [(t.booking_type, t.amount_cents) for t in stmt.transactions] == [
        ("Lastschrift", -4510),
        ("Gutschrift", 250000),
        ("Dauerauftrag", -90000),
    ]
    assert stmt.transactions[0].purpose == "Einkauf Filiale 123"


def test_example_as_user_plugin_imports_the_sample(app, tmp_path):
    from hiro_budget.services import import_service

    folder = app.settings.plugins_dir / "musterbank_pdf"
    folder.mkdir(parents=True)
    (folder / "plugin.yaml").write_text(block("## 4. The API", "yaml"), encoding="utf-8")
    (folder / "importer.py").write_text(block("## 5. Worked example", "python"), "utf-8")
    app.settings.app.plugins.enabled = ["musterbank_pdf"]
    pdf = write_statement_pdf(tmp_path / "musterbank.pdf", sample_pages())
    with app.session() as s:
        result = import_service.import_file(s, app.settings, pdf)
        assert result.added == 3
        again = write_statement_pdf(tmp_path / "copy.pdf", [*sample_pages(), ""])
        assert import_service.preview(s, again, settings=app.settings).new_count == 0
