"""The start of the app stays light: heavy libraries load when they are needed."""

import subprocess
import sys

from alembic.script import ScriptDirectory

from hiro_budget.db.session import (
    MIGRATIONS_DIR,
    alembic_config,
    current_revision,
    head_revision,
    make_engine,
    upgrade_db,
)


def test_head_revision_is_alembics_head():
    engine = make_engine(":memory:")
    cfg = alembic_config(engine)
    assert cfg.get_main_option("script_location") == str(MIGRATIONS_DIR)
    assert head_revision() == ScriptDirectory.from_config(cfg).get_current_head()


def test_upgrade_creates_then_skips(tmp_path):
    engine = make_engine(tmp_path / "a.db")
    assert current_revision(engine) is None
    upgrade_db(engine)
    assert current_revision(engine) == head_revision()
    upgrade_db(engine)  # nothing to do, no Alembic needed


def test_no_heavy_imports_at_start():
    code = (
        "import sys; import hiro_budget.app, hiro_budget.gui.main_window;"
        "print(sorted(m for m in ('sklearn', 'scipy', 'numpy', 'joblib', 'pdfplumber',"
        " 'openpyxl', 'alembic', 'fints', 'hiro_budget.importers.plugins')"
        " if m in sys.modules))"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "[]"
