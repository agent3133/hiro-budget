"""Backups of the database and the configuration.

A backup is a folder ``<database folder>/backups/<date>_<time>_<reason>/`` with a copy
of the database (made with SQLite's backup API, so it is consistent while the app has
it open) and of config.yaml, categories.yaml and rules.yaml. One is made before every
import; the newest ``backup.keep`` are kept.

Restoring (File > Restore backup…, #48): the current state is backed up first ("restore"),
then, after the window closed, ``restore_backup`` copies the database and the YAML files
back; where the data lives (``data_folder``, ``database.path``, ``ml.model_path``) stays as
it is now. By hand: close the app and copy the files of a backup folder back (the database
next to the backups folder, the YAML files into the config folder).
"""

from __future__ import annotations

import re
import shutil
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from hiro_budget.config import Settings, load_yaml, save_yaml

NAME = re.compile(r"^(\d{4}-\d{2}-\d{2}_\d{6})(?:-(\d+))?_([a-z]+)$")


@dataclass
class Backup:
    path: Path
    created: datetime
    reason: str  # "import", "manual", "restore" (made before restoring another one)
    number: int = 1  # several backups within the same second


def backup_dir(settings: Settings) -> Path:
    return settings.data_dir / "backups"


def create_backup(settings: Settings, reason: str = "manual", prune_old: bool = True) -> Path:
    """Copy database and configuration into a new backup folder; prune old ones (not
    before a restore: the backup to restore may be the oldest)."""
    now = datetime.now().replace(microsecond=0)
    # several backups within the same second: numbered after the highest one there
    same_second = [b.number for b in list_backups(settings) if b.created == now]
    number = max(same_second, default=0) + 1
    suffix = f"-{number}" if number > 1 else ""
    folder = backup_dir(settings) / f"{now:%Y-%m-%d_%H%M%S}{suffix}_{reason}"
    folder.mkdir(parents=True)
    try:
        db = settings.db_path
        if db.exists():
            with (
                closing(sqlite3.connect(db)) as source,
                closing(sqlite3.connect(folder / db.name)) as target,
            ):
                source.backup(target)
        config = folder / "config"
        config.mkdir()
        for f in (
            settings.config_dir / "config.yaml",
            settings.categories_file,
            settings.rules_file,
        ):
            if f.exists():
                shutil.copy2(f, config / f.name)
    except Exception:
        shutil.rmtree(folder, ignore_errors=True)  # no half backups
        raise
    if prune_old:
        prune(settings)
    return folder


def list_backups(settings: Settings) -> list[Backup]:
    """Backups, newest first."""
    folder = backup_dir(settings)
    if not folder.is_dir():
        return []
    found = []
    for p in folder.iterdir():
        m = NAME.match(p.name)
        if p.is_dir() and m:
            created = datetime.strptime(m[1], "%Y-%m-%d_%H%M%S")
            found.append(Backup(p, created, m[3], int(m[2] or 1)))
    return sorted(found, key=lambda b: (b.created, b.number), reverse=True)


def prune(settings: Settings) -> None:
    """Remove all but the newest ``backup.keep`` backups (only folders named by us)."""
    for old in list_backups(settings)[settings.app.backup.keep :]:
        shutil.rmtree(old.path, ignore_errors=True)


#: where the data lives: kept from the current config.yaml when restoring one
LOCATION_KEYS = (("data_folder",), ("database", "path"), ("ml", "model_path"))


def backup_size(b: Backup) -> int:
    """Bytes of all files in the backup."""
    return sum(f.stat().st_size for f in b.path.rglob("*") if f.is_file())


def backup_bookings(settings: Settings, b: Backup) -> int | None:
    """Bookings in the backup's database (None: no readable database in it)."""
    db = b.path / settings.db_path.name
    if not db.is_file():
        return None
    try:
        with closing(sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)) as con:
            return con.execute('select count(*) from "transaction"').fetchone()[0]
    except sqlite3.Error:
        return None


def restore_backup(settings: Settings, folder: Path) -> None:
    """Copy the backup's database and YAML files back (the app must not have the database
    open). Its config.yaml keeps the current ``data_folder``, ``database.path`` and
    ``ml.model_path``: an older backup must not move the data elsewhere."""
    db = folder / settings.db_path.name
    if not db.is_file():
        raise FileNotFoundError(db)
    with (
        closing(sqlite3.connect(db)) as source,
        closing(sqlite3.connect(settings.db_path)) as target,
    ):
        source.backup(target)
    config = folder / "config"
    current = load_yaml(settings.config_file) if settings.config_file.exists() else {}
    for file in (settings.config_file, settings.categories_file, settings.rules_file):
        copy = config / file.name
        if copy.is_file():
            shutil.copy2(copy, file)
    if (config / settings.config_file.name).is_file():
        doc = load_yaml(settings.config_file)
        for keys in LOCATION_KEYS:
            _keep(doc, current, keys)
        save_yaml(settings.config_file, doc)


def _keep(doc, current, keys: tuple[str, ...]) -> None:
    """``doc[keys]`` as in ``current`` (removed when ``current`` has none)."""
    *parents, last = keys
    for key in parents:
        current = (current or {}).get(key)
        if doc.get(key) is None:
            if current is None or last not in current:
                return
            doc[key] = {}
        doc = doc[key]
    if current is not None and last in current:
        doc[last] = current[last]
    else:
        doc.pop(last, None)
