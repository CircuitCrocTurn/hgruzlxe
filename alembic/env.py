"""Alembic environment for the Offerday project.

We run Alembic against the same async ``DATABASE_URL`` that the FastAPI
app uses.  Async engines do not plug directly into Alembic's classic
``run_migrations_online`` callback, so we use the ``async_engine``
helpers from SQLAlchemy 2.0 and bridge into Alembic via ``run_sync``.

Models are imported through :mod:`app.db.models` so every table is
registered on ``Base.metadata`` before autogenerate runs.
"""
from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.config import get_settings
from app.db.base import Base
from app.db import models  # noqa: F401  — registers all tables on Base.metadata


config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Pull the DSN from the app's settings so that ``.env`` is the only
# source of truth.  Override the alembic.ini value at runtime.
config.set_main_option("sqlalchemy.url", get_settings().database_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations without connecting to the database.

    Generates SQL to stdout — useful for code review and for handing
    DDL over to a DBA.
    """
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Open an async engine, then bridge into Alembic via ``run_sync``."""
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
