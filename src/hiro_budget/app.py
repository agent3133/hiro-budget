"""Application context shared by CLI and GUI: settings + database."""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy.orm import Session, sessionmaker

from hiro_budget.config import CSV_FORMATS_FILE, Settings, load_settings
from hiro_budget.db.session import open_database
from hiro_budget.services.config_edit import KeyRenames
from hiro_budget.services.counterparts import sync_all_counterparts
from hiro_budget.services.sync import rename_keys, sync_accounts, sync_categories
from hiro_budget.ui_state import UiState

_FINTS_SECTION = re.compile(r"^fints:", re.MULTILINE)


def _migrate_fints_section(settings: Settings) -> None:
    """The ``fints`` section (up to 2026-11) becomes ``bank_access`` in config.yaml (#82);
    the loaded settings have it converted already."""
    try:
        text = settings.config_file.read_text(encoding="utf-8")
    except OSError:
        return
    if _FINTS_SECTION.search(text):
        from hiro_budget.services.config_edit import migrate_bank_access

        migrate_bank_access(settings.config_file)
        logging.getLogger(__name__).info("config.yaml: fints section moved to bank_access")


class App:
    def __init__(self, settings: Settings, db_path: Path | str | None = None) -> None:
        self.settings = settings
        if (settings.config_dir / CSV_FORMATS_FILE).exists():  # before plugins (#24)
            from hiro_budget.importers.plugins import migrate_csv_formats

            migrate_csv_formats(settings.config_dir)
        _migrate_fints_section(settings)
        db = Path(db_path or settings.db_path)
        self.session_factory: sessionmaker[Session] = open_database(db)
        self.ui_state = UiState(db.parent / "ui_state.json")
        self.restart = None  # a RestartRequest once the data folder was moved (#29)
        self.sync_config()

    @classmethod
    def from_config_dir(cls, config_dir: Path | None = None) -> App:
        return cls(load_settings(config_dir))

    @contextmanager
    def session(self) -> Iterator[Session]:
        """Session that commits on success and rolls back on error."""
        with self.session_factory() as s:
            try:
                yield s
                s.commit()
            except Exception:
                s.rollback()
                raise

    def sync_config(self, renames: KeyRenames | None = None) -> None:
        """Push categories and own accounts from YAML into the database.

        ``renames``: category/group keys changed in YAML; the rows are renamed first so
        that bookings and budgets stay with them.
        """
        with self.session() as s:
            if renames:
                rename_keys(s, renames.groups, renames.categories)
            sync_categories(s, self.settings.categories)
            sync_accounts(s, self.settings.app.own_accounts)
            sync_all_counterparts(s, self.settings.app.matching.days)
            model = self.settings.model_path
            # a model of the scikit-learn version (classifier.joblib) is made again once
            outdated = not model.exists() and model.with_suffix(".joblib").exists()
            renamed = bool(renames and renames.categories)  # it predicts category keys
            if self.settings.app.ml.enabled and (renamed or outdated):
                from hiro_budget.services.categorization.pipeline import train_classifier

                train_classifier(s, self.settings)

    def reload_config(self) -> None:
        self.settings = load_settings(self.settings.config_dir)
        self.sync_config()
