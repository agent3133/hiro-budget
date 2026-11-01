"""Backups of the database and the configuration.

A backup is a folder ``<database folder>/backups/<date>_<time>_<reason>/`` with a copy
of the database (made with SQLite's backup API, so it is consistent while the app has
it open) and of config.yaml, categories.yaml and rules.yaml. One is made before every
import; the newest ``backup.keep`` are kept.

Restoring: close the app and copy the files of a backup folder back (the database next
to the backups folder, the YAML files into the config folder).
"""

from __future__ import annotations

import re
import shutil
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from hiro_budget.config import Settings

NAME = re.compile(r"^(\d{4}-\d{2}-\d{2}_\d{6})(?:-(\d+))?_([a-z]+)$")


@dataclass
class Backup:
    path: Path
    created: datetime
    reason: str  # "import", "manual"
    number: int = 1  # several backups within the same second


def backup_dir(settings: Settings) -> Path:
    return settings.db_path.parent / "backups"


def create_backup(settings: Settings, reason: str = "manual") -> Path:
    """Copy database and configuration into a new backup folder; prune old ones."""
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
