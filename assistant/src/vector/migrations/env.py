"""Alembic environment for the vector store (async engine).

URL resolution order:
1. `config.attributes["database_url"]` — set by `vector.db.run_migrations`
   (avoids configparser `%` interpolation issues with passwords);
2. `ALEMBIC_DATABASE_URL` env var — CLI escape hatch;
3. `sqlalchemy.url` from alembic.ini (normally unset);
4. `Settings.database_url` — the app's own bootstrap config (.env / env vars).

Concurrent replicas racing `upgrade head` are not handled — the assistant
runs as a single replica today; wrap this in pg_advisory_lock if that changes.
"""

import asyncio
import os
import sys
from pathlib import Path

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

# Allow running via the alembic CLI from assistant/ (the app itself always
# has src/ on the path).
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from vector.models import Base  # noqa: E402

alembic_config = context.config
target_metadata = Base.metadata


def _database_url() -> str:
    url = (
        alembic_config.attributes.get("database_url")
        or os.environ.get("ALEMBIC_DATABASE_URL")
        or alembic_config.get_main_option("sqlalchemy.url")
    )
    if not url:
        from config import get_settings  # the app's config module, not alembic's

        url = get_settings().database_url
    if not url:
        raise RuntimeError("No database URL: set DATABASE_URL (app config) or ALEMBIC_DATABASE_URL")
    return url


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def _run_async_migrations() -> None:
    engine = create_async_engine(_database_url(), poolclass=pool.NullPool)
    async with engine.connect() as connection:
        await connection.run_sync(_do_run_migrations)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(_run_async_migrations())
