"""``hiro-budget demo``: the app on ten years of a fictional family's finances.

The demo (``demo/config`` plus the generated ``demo/data``) is copied to a temporary folder
and opened there: it starts fresh every time, nothing of the user's is read or changed.
It is found next to the exe (a release ships it generated) or in the repository's ``demo/``
folder; in a checkout it is generated there first (``demo/generate.py``, needs Faker:
``uv sync``; about 20 seconds).
"""

from __future__ import annotations

import importlib.util
import shutil
import sys
import tempfile
from pathlib import Path

DB = Path("data") / "demo.db"  # as demo/config/config.yaml says


def _candidates() -> list[Path]:
    places = [Path(sys.argv[0]).resolve().parent / "demo"]  # next to the exe
    places.append(Path(__file__).resolve().parents[2] / "demo")  # src/hiro_budget -> repo
    return [p for p in places if (p / "config" / "config.yaml").is_file()]


def _generate(demo: Path) -> None:
    script = demo / "generate.py"
    if not script.is_file():
        raise RuntimeError(f"the demo has no database and no generator: {demo}")
    print("Generating the demo database (about 20 seconds)...", flush=True)
    spec = importlib.util.spec_from_file_location("hiro_budget_demo_generate", script)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look their module up while it loads
    try:
        spec.loader.exec_module(module)
    except ModuleNotFoundError as e:
        raise RuntimeError(f"the demo generator needs {e.name}: run `uv sync` first") from e
    module.generate(demo / DB, quiet=True)


def prepare(target: Path | None = None, source: Path | None = None) -> Path:
    """A fresh copy of the demo (``source``: the demo folder, else found as above);
    returns its config folder."""
    found = [source] if source is not None else _candidates()
    if not found:
        raise RuntimeError("no demo found (a folder 'demo' next to the program)")
    demo = found[0]
    if not (demo / DB).is_file():
        _generate(demo)
    target = target or Path(tempfile.gettempdir()) / "hiro-budget-demo"
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)
    if target.exists():  # still open in another demo window
        target = Path(tempfile.mkdtemp(prefix="hiro-budget-demo-"))
    shutil.copytree(demo / "config", target / "config")
    shutil.copytree(  # database, trained classifier, window state
        demo / DB.parent, target / DB.parent, ignore=shutil.ignore_patterns("backups")
    )
    return target / "config"
