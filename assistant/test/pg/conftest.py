"""Postgres integration tests (real pgvector via Testcontainers).

Not collected by the default `uv run pytest` (pytest.toml testpaths is
test/unit) — run explicitly with `uv run pytest test/pg`. Requires Docker;
skips the whole session when it is unavailable.

One container per session; per-test isolation is done in the `engine`
fixture teardown: static tables are truncated and any versioned chunk
tables are dropped.
"""

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from vector.db import run_migrations


@pytest.fixture(scope="session")
def pg_container():
    try:
        from testcontainers.postgres import PostgresContainer

        container = PostgresContainer("pgvector/pgvector:pg17", driver="asyncpg")
        container.start()
    except Exception as e:  # docker missing, daemon down, image pull failure …
        pytest.skip(f"Docker/Testcontainers unavailable: {e}")
    yield container
    container.stop()


@pytest.fixture(scope="session")
def database_url(pg_container) -> str:
    return pg_container.get_connection_url()


@pytest.fixture(scope="session")
def migrated(database_url: str) -> str:
    run_migrations(database_url)
    return database_url


@pytest.fixture
async def engine(migrated: str):
    eng: AsyncEngine = create_async_engine(migrated)
    yield eng
    async with eng.begin() as conn:
        chunk_tables = await conn.execute(
            text("SELECT tablename FROM pg_tables WHERE tablename LIKE 'vector\\_chunk\\_v%'")
        )
        for (name,) in chunk_tables:
            await conn.execute(text(f'DROP TABLE IF EXISTS "{name}"'))
        await conn.execute(text("TRUNCATE vector_index_meta, vector_sync_state, index_journal"))
    await eng.dispose()
