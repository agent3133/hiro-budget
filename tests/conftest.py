from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# GUI tests run headless.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# The headless platform has no fonts of its own; with the system fonts, text is measured
# as in the application (window size tests).
_WINDOWS_FONTS = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
if _WINDOWS_FONTS.is_dir():
    os.environ.setdefault("QT_QPA_FONTDIR", str(_WINDOWS_FONTS))

from hiro_budget.config import TEMPLATES  # noqa: E402
from tests.fixtures import ing_statement  # noqa: E402 - after the Qt environment above

REPO_ROOT = Path(__file__).resolve().parent.parent
REAL_INPUT_DIR = REPO_ROOT / "input"


@pytest.hookimpl(wrapper=True)
def pytest_runtest_teardown(item):
    """Really delete the widgets of the test. pytest-qt closes them with deleteLater(),
    which only an event loop carries out (processEvents() does not): without this, every
    window of every test stays alive, and each switch of the theme restyles all of them.

    Classifier trainings still running in the background (#55) finish first, while the
    widgets that wait for them are still there."""
    training = sys.modules.get("hiro_budget.gui.training")
    if training is not None:
        training.wait_all()
    result = yield
    from PySide6.QtCore import QCoreApplication, QEvent

    if QCoreApplication.instance() is not None:
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    return result


def write_statement_pdf(path: Path, pages: list[str]) -> Path:
    """Render statement text pages into a simple PDF (one text line per row)."""
    from fpdf import FPDF

    pdf = FPDF(format="A4")
    pdf.set_auto_page_break(False)
    pdf.set_font("Helvetica", size=9)
    for text in pages:
        pdf.add_page()
        y = 15
        for line in text.splitlines():
            pdf.set_xy(15, y)
            pdf.cell(text=line)
            y += 5
    pdf.output(str(path))
    return path


@pytest.fixture
def statement_pdf(tmp_path: Path) -> Path:
    return write_statement_pdf(tmp_path / "Girokonto_Test.pdf", ing_statement.PAGES)


@pytest.fixture
def real_statements() -> list[Path]:
    pdfs = sorted(REAL_INPUT_DIR.glob("*.pdf")) if REAL_INPUT_DIR.is_dir() else []
    if not pdfs:
        pytest.skip("no real statements in input/")
    return pdfs


TEST_CONFIG = """\
database:
  path: data/test.db
language: en
own_accounts:
  - name: ING Girokonto
    kind: giro
    institution: ING
    iban: DE12 3456 7890 1234 5678 90
  - name: ING Extra-Konto
    kind: savings
    match:
      purpose: "(?i)extra-?konto"
  - name: ING Baufinanzierung
    kind: loan
  - name: Bargeld
    kind: cash
    opening_balance: 100.0
    opening_date: 2026-03-01
mortgage:
  - name: Baufinanzierung
    loan_account: ING Baufinanzierung
    interest_category: wohnen.baufinanzierung
    match:
      purpose: "(?i)ZINSEN.*TILGUNG"
ml:
  enabled: true
  min_samples: 10
  min_confidence: 0.5
  model_path: data/classifier.npz
"""


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    """A config directory with the committed example categories/rules and a test config."""
    import shutil

    cfg = tmp_path / "config"
    cfg.mkdir()
    for name in ("categories", "rules"):
        shutil.copyfile(TEMPLATES / f"{name}.example.yaml", cfg / f"{name}.yaml")
    (cfg / "config.yaml").write_text(TEST_CONFIG, encoding="utf-8")
    return cfg


@pytest.fixture
def app(config_dir: Path):
    from hiro_budget.app import App

    return App.from_config_dir(config_dir)


# -- scenario modules: ordered steps; after a failing step the later ones are skipped -----

_failed_step: dict[str, str] = {}


def pytest_runtest_makereport(item, call):
    if "incremental" in item.keywords and call.excinfo is not None:
        if not call.excinfo.errisinstance(pytest.skip.Exception):
            _failed_step.setdefault(item.module.__name__, item.name)


def pytest_runtest_setup(item):
    if "incremental" in item.keywords and (failed := _failed_step.get(item.module.__name__)):
        pytest.skip(f"an earlier step failed: {failed}")
