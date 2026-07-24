"""Stage 0 smoke: migrations apply, pgvector works end-to-end."""

from sqlalchemy import text

from vector.db import run_migrations

_STATIC_TABLES = {"vector_index_meta", "vector_sync_state", "index_journal"}


async def test_pgvector_roundtrip(engine):
    async with engine.connect() as conn:
        vec = (await conn.execute(text("SELECT '[1,2,3]'::vector"))).scalar_one()
        assert str(vec) == "[1,2,3]"
        half = (await conn.execute(text("SELECT '[0.5,1.5,2.5]'::halfvec"))).scalar_one()
        assert str(half) == "[0.5,1.5,2.5]"
        distance = (await conn.execute(text("SELECT '[1,0]'::halfvec <=> '[0,1]'::halfvec"))).scalar_one()
        assert distance == 1.0  # cosine distance of orthogonal vectors


async def test_migration_creates_tables(engine):
    async with engine.connect() as conn:
        tables = (await conn.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'"))).scalars()
        assert _STATIC_TABLES <= set(tables)
        extensions = (await conn.execute(text("SELECT extname FROM pg_extension"))).scalars()
        assert "vector" in set(extensions)


def test_migration_idempotent(migrated: str):
    run_migrations(migrated)  # second upgrade to head is a no-op, no error
