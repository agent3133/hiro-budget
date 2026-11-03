"""Alembic environment. Uses the engine passed in by ``db.session`` when available."""

from alembic import context
from sqlalchemy import create_engine

from hiro_budget.db.models import Base

config = context.config
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        render_as_batch=True,
        literal_binds=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    url = config.get_main_option("sqlalchemy.url")
    engine = config.attributes.get("engine")
    if engine is None:
        assert url is not None, "no engine and no sqlalchemy.url"
        engine = create_engine(url)
    with engine.connect() as connection:
        # Batch migrations on SQLite recreate tables (copy, drop, rename). With foreign
        # keys enabled, dropping a table would cascade-delete referencing rows (e.g. all
        # splits when "transaction" is rebuilt). The pragma only works outside a
        # transaction, so it is set before the migration transaction begins.
        is_sqlite = connection.dialect.name == "sqlite"
        if is_sqlite:
            connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
            connection.commit()  # end the autobegun transaction; the pragma stays in effect
        try:
            with connection.begin():
                context.configure(
                    connection=connection, target_metadata=target_metadata, render_as_batch=True
                )
                context.run_migrations()
            if is_sqlite:
                problems = connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
                connection.commit()
                if problems:
                    raise RuntimeError(f"foreign key violations after migration: {problems}")
        finally:
            if is_sqlite:
                connection.rollback()
                connection.exec_driver_sql("PRAGMA foreign_keys=ON")
                connection.commit()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
