"""Move the data folder (#29): database, classifier, backups and view state together.

The database is copied with SQLite's backup API (consistent while the app has it open),
then checked in the new place (integrity check, row counts of every table); the other
files are copied and compared by size. Anything created in the new folder is removed again
when a step fails, and nothing is written to config.yaml before all checks passed. The old
files stay until the user deletes them (``delete_files``), after the app closed them.
"""

from __future__ import annotations

import shutil
import sqlite3
import uuid
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path

from hiro_budget.config import Settings

UI_STATE = "ui_state.json"


class DataFolderError(Exception):
    """A move that cannot be done or failed; the old folder stays in use. ``kind`` names
    the reason for a translated message, ``detail`` is filled into it."""

    def __init__(self, kind: str, message: str, detail: str = "") -> None:
        super().__init__(message)
        self.kind = kind  # same, inside, not_folder, clash, not_writable, no_space,
        self.detail = detail  # damaged, differs, failed


@dataclass
class DataFiles:
    """What lives in a data folder (file names as in the current configuration)."""

    database: Path
    classifier: Path
    backups: Path
    ui_state: Path

    def all(self) -> list[Path]:
        return [self.database, self.classifier, self.backups, self.ui_state]

    def existing(self) -> list[Path]:
        return [p for p in self.all() if p.exists()]


@dataclass
class MoveResult:
    target: Path
    old_files: list[Path]  # what was copied, in the old folder (for deleting later)
    rows: int  # rows in the copied database
    backups: int  # backup folders copied
    size: int  # bytes copied


@dataclass
class RestartRequest:
    """What to do after the window closed, before the app starts again (#29)."""

    delete: list[Path]  # old data files the user chose to delete
    ui_state: tuple[Path, Path] | None  # view state written on closing: old -> new place
    restore: Path | None = None  # a backup folder to copy back (#48)


def data_files(settings: Settings, folder: Path | None = None) -> DataFiles:
    folder = settings.data_dir if folder is None else folder
    return DataFiles(
        database=folder / settings.db_path.name,
        classifier=folder / settings.model_path.name,
        backups=folder / "backups",
        ui_state=folder / UI_STATE,
    )


def holds_database(settings: Settings, folder: Path) -> bool:
    """A folder with a Hiro Budget database of this configuration's name (open it there)."""
    return (folder / settings.db_path.name).is_file()


def size_of(path: Path) -> int:
    if path.is_dir():
        return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
    return path.stat().st_size if path.exists() else 0


def check_target(settings: Settings, target: Path) -> None:
    """Raise DataFolderError if the files cannot be moved to ``target``."""
    current = settings.data_dir.resolve()
    target = target.resolve()
    if target == current:
        raise DataFolderError("same", "This is already the data folder.")
    if target.is_relative_to(current):
        raise DataFolderError(
            "inside", "The new folder must not be inside the current data folder."
        )
    if target.exists() and not target.is_dir():
        raise DataFolderError("not_folder", f"{target} is not a folder.", str(target))
    clashes = [p.name for p in data_files(settings, target).all() if p.exists()]
    if clashes:
        names = ", ".join(clashes)
        raise DataFolderError("clash", f"The folder already holds {names}.", names)
    try:
        target.mkdir(parents=True, exist_ok=True)
        probe = target / f".hiro-write-test-{uuid.uuid4().hex}"
        probe.write_bytes(b"")
        probe.unlink()
    except OSError as e:
        raise DataFolderError("not_writable", f"Cannot write to {target}: {e}", str(e)) from e
    needed = sum(size_of(p) for p in data_files(settings).existing())
    free = shutil.disk_usage(target).free
    if needed > free:
        detail = f"{needed / 1e6:.1f} MB / {free / 1e6:.1f} MB"
        raise DataFolderError("no_space", f"Not enough space in {target}: {detail}", detail)


def _table_counts(db: Path) -> dict[str, int]:
    uri = f"file:{db.as_posix()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as con:
        if con.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise DataFolderError("damaged", f"The copy of the database in {db.parent} is damaged.")
        tables = [
            r[0]
            for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        return {t: con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in tables}


def _files(folder: Path) -> dict[str, int]:
    return {
        f.relative_to(folder).as_posix(): f.stat().st_size for f in folder.rglob("*") if f.is_file()
    }


def move_data(
    settings: Settings, target: Path, progress: Callable[[str], None] | None = None
) -> MoveResult:
    """Copy the data files to ``target`` and check the copies. Does not touch config.yaml
    and leaves the old files in place."""
    say = progress or (lambda _text: None)
    check_target(settings, target)
    target = target.resolve()
    old, new = data_files(settings), data_files(settings, target)
    created: list[Path] = []
    try:
        rows = 0
        if old.database.exists():
            say("database")
            created.append(new.database)
            with (
                closing(sqlite3.connect(old.database)) as source,
                closing(sqlite3.connect(new.database)) as copy,
            ):
                source.backup(copy)
            before, after = _table_counts(old.database), _table_counts(new.database)
            if before != after:
                raise DataFolderError(
                    "differs", "The copy of the database differs from the original.", "database"
                )
            rows = sum(after.values())
        for src, dst in ((old.classifier, new.classifier), (old.ui_state, new.ui_state)):
            if src.exists():
                say(src.name)
                created.append(dst)
                shutil.copy2(src, dst)
                if dst.stat().st_size != src.stat().st_size:
                    raise DataFolderError(
                        "differs", f"The copy of {src.name} differs from the original.", src.name
                    )
        backups = 0
        if old.backups.is_dir():
            say("backups")
            created.append(new.backups)
            shutil.copytree(old.backups, new.backups)
            if _files(new.backups) != _files(old.backups):
                raise DataFolderError(
                    "differs", "The copied backups differ from the originals.", "backups"
                )
            backups = sum(1 for p in new.backups.iterdir() if p.is_dir())
    except Exception as e:
        for path in reversed(created):  # no half data folder
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            else:
                path.unlink(missing_ok=True)
        if isinstance(e, DataFolderError):
            raise
        raise DataFolderError("failed", f"Copying to {target} failed: {e}", str(e)) from e
    copied = old.existing()
    return MoveResult(
        target=target,
        old_files=copied,
        rows=rows,
        backups=backups,
        size=sum(size_of(p) for p in copied),
    )


@dataclass
class RestartOutcome:
    restore_error: str = ""  # the backup could not be restored (the data are as they were)
    delete_problems: list[str] = field(default_factory=list)  # old files left behind


def apply_restart(settings: Settings, request: RestartRequest) -> RestartOutcome:
    """The file work after the window closed and the database was released, before the
    app starts again: restore a backup, carry over the view state written on closing,
    delete the old data files if asked."""
    outcome = RestartOutcome()
    if request.restore is not None:  # File > Restore backup… (#48)
        from hiro_budget.services import backup

        try:
            backup.restore_backup(settings, request.restore)
        except Exception as e:  # the app starts on the data as it was
            outcome.restore_error = str(e)
    if request.ui_state is not None:
        old, new = request.ui_state
        if old.exists() and old != new:
            shutil.copy2(old, new)
    outcome.delete_problems = delete_files(request.delete)
    return outcome


def delete_files(paths: list[Path]) -> list[str]:
    """Delete exactly these files and folders (the old data); the problems as text."""
    problems = []
    for path in paths:
        try:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink(missing_ok=True)
        except OSError as e:
            problems.append(f"{path}: {e}")
    return problems
