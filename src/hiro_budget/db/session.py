"""Engine/session creation and schema migration on startup."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

if TYPE_CHECKING:
    from alembic.config import Config as AlembicConfig

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def make_engine(db_path: Path | str) -> Engine:
    """Create an engine for a SQLite file (or ``:memory:``) with foreign keys enabled."""
    if str(db_path) != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{db_path}")

    @event.listens_for(engine, "connect")
    def _pragmas(dbapi_conn, _record):  # noqa: ANN001
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    return engine


def alembic_config(engine: Engine) -> AlembicConfig:
    # Alembic logs plugin setup (at import time) and every migration step at INFO;
    # keep the app log readable. Must run before alembic is imported.
    logging.getLogger("alembic").setLevel(logging.WARNING)
    from alembic.config import Config as AlembicConfig

    cfg = AlembicConfig()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option(
        "sqlalchemy.url", engine.url.render_as_string(hide_password=False).replace("%", "%%")
    )
    cfg.attributes["engine"] = engine
    return cfg


def head_revision() -> str:
    """The newest migration; the revisions are numbered in order ("0005_….py")."""
    return max(p.name[:4] for p in (MIGRATIONS_DIR / "versions").glob("[0-9][0-9][0-9][0-9]_*.py"))


def current_revision(engine: Engine) -> str | None:
    try:
        with engine.connect() as conn:
            return conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
    except OperationalError:  # a new database
        return None


def upgrade_db(engine: Engine) -> None:
    """Bring the database schema to the latest revision. Alembic (~0.2 s to import) is
    only loaded when there is something to migrate."""
    if current_revision(engine) == head_revision():
        return
    from alembic import command

    command.upgrade(alembic_config(engine), "head")


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(engine, expire_on_commit=False)


def open_database(db_path: Path | str) -> sessionmaker[Session]:
    engine = make_engine(db_path)
    upgrade_db(engine)
    return make_session_factory(engine)
