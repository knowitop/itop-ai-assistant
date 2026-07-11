"""VectorIndex integration tests: the SQL/pgvector seam against real Postgres."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from vector.db import VectorDb
from vector.index import ChunkRecord, FingerprintMismatchError, VectorIndex

_MODEL = "test-model"
_DIM = 4
_CREATED = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)


@pytest.fixture
async def db(migrated: str, engine):
    """VectorDb wired to the session container; `engine` handles cleanup."""
    vdb = VectorDb(migrated)
    yield vdb
    await vdb.aclose()


@pytest.fixture
def index(db) -> VectorIndex:
    return VectorIndex(db, env="main")


def _chunk(
    obj_id: int,
    embedding: list[float],
    *,
    obj_class: str = "UserRequest",
    chunk_kind: str = "description",
    chunk_n: int = 0,
    visibility: str = "public",
    status: str = "resolved",
    org_id: str | None = "org-1",
    content_hash: str = "hash-0",
) -> ChunkRecord:
    return ChunkRecord(
        obj_class=obj_class,
        obj_id=obj_id,
        chunk_kind=chunk_kind,
        chunk_n=chunk_n,
        visibility=visibility,
        status=status,
        org_id=org_id,
        service_id=None,
        content_hash=content_hash,
        embedding=embedding,
        created_at=_CREATED,
    )


class TestEnsureVersion:
    async def test_creates_v1_with_table_and_indexes(self, index, db):
        meta = await index.ensure_version(_MODEL, _DIM)

        assert (meta.version, meta.model, meta.dim) == (1, _MODEL, _DIM)
        async with db.connect() as conn:
            index_names = set(
                (
                    await conn.execute(text("SELECT indexname FROM pg_indexes WHERE tablename = 'vector_chunk_v1'"))
                ).scalars()
            )
        assert "vector_chunk_v1_emb_hnsw" in index_names
        assert "vector_chunk_v1_filter" in index_names
        assert "vector_chunk_v1_org" in index_names
        assert "vector_chunk_v1_obj" in index_names

    async def test_second_call_is_noop(self, index):
        first = await index.ensure_version(_MODEL, _DIM)
        second = await index.ensure_version(_MODEL, _DIM)
        assert first == second
        assert await index.active_meta() == first

    async def test_fingerprint_mismatch_raises(self, index):
        await index.ensure_version(_MODEL, _DIM)
        with pytest.raises(FingerprintMismatchError):
            await index.ensure_version("other-model", _DIM)
        with pytest.raises(FingerprintMismatchError):
            await index.ensure_version(_MODEL, 8)

    async def test_no_version_initially(self, index):
        assert await index.active_meta() is None
        assert await index.stats() is None
        assert (
            await index.search([0.0] * _DIM, classes=["UserRequest"], statuses=["resolved"], visibilities=["public"])
            == []
        )


class TestUpsert:
    async def test_upsert_then_update_keeps_one_row(self, index, db):
        await index.ensure_version(_MODEL, _DIM)

        inserted = await index.upsert_chunks([_chunk(1, [1.0, 0.0, 0.0, 0.0])], model=_MODEL, dim=_DIM)
        assert inserted == 1

        # Same key, new embedding/hash → ON CONFLICT update, still one row
        await index.upsert_chunks([_chunk(1, [0.0, 1.0, 0.0, 0.0], content_hash="hash-1")], model=_MODEL, dim=_DIM)
        async with db.connect() as conn:
            rows = (await conn.execute(text("SELECT count(*), max(content_hash) FROM vector_chunk_v1"))).one()
        assert rows == (1, "hash-1")

    async def test_write_requires_matching_fingerprint(self, index):
        await index.ensure_version(_MODEL, _DIM)
        with pytest.raises(FingerprintMismatchError):
            await index.upsert_chunks([_chunk(1, [0.0] * _DIM)], model="other-model", dim=_DIM)

    async def test_write_without_version_raises(self, index):
        with pytest.raises(FingerprintMismatchError):
            await index.upsert_chunks([_chunk(1, [0.0] * _DIM)], model=_MODEL, dim=_DIM)

    async def test_empty_upsert_is_noop(self, index):
        assert await index.upsert_chunks([], model=_MODEL, dim=_DIM) == 0


class TestSearch:
    async def test_nearest_first_and_max_aggregation(self, index):
        await index.ensure_version(_MODEL, _DIM)
        query = [1.0, 0.0, 0.0, 0.0]
        await index.upsert_chunks(
            [
                # Object 1: two chunks — one far, one near; max must win once
                _chunk(1, [1.0, 0.1, 0.0, 0.0], chunk_kind="description"),
                _chunk(1, [0.0, 0.0, 1.0, 0.0], chunk_kind="solution"),
                # Object 2: exact match
                _chunk(2, [1.0, 0.0, 0.0, 0.0]),
                # Object 3: orthogonal
                _chunk(3, [0.0, 1.0, 0.0, 0.0]),
            ],
            model=_MODEL,
            dim=_DIM,
        )

        hits = await index.search(query, classes=["UserRequest"], statuses=["resolved"], visibilities=["public"])

        assert [hit.obj_id for hit in hits] == [2, 1, 3]  # nearest first, one hit per object
        assert hits[0].score == pytest.approx(1.0, abs=1e-3)
        assert hits[0].score >= hits[1].score >= hits[2].score

    async def test_filters(self, index):
        await index.ensure_version(_MODEL, _DIM)
        vec = [1.0, 0.0, 0.0, 0.0]
        await index.upsert_chunks(
            [
                _chunk(1, vec),
                _chunk(2, vec, status="new"),
                _chunk(3, vec, visibility="internal"),
                _chunk(4, vec, org_id="org-2"),
                _chunk(5, vec, obj_class="Incident"),
            ],
            model=_MODEL,
            dim=_DIM,
        )

        async def ids(**kwargs) -> set[int]:
            defaults = {"classes": ["UserRequest"], "statuses": ["resolved"], "visibilities": ["public"]}
            hits = await index.search(vec, **{**defaults, **kwargs})
            return {hit.obj_id for hit in hits}

        assert await ids() == {1, 4}  # unrestricted orgs; status/visibility/class filtered
        assert await ids(statuses=["resolved", "new"]) == {1, 2, 4}
        assert await ids(visibilities=["public", "internal"]) == {1, 3, 4}
        assert await ids(classes=["UserRequest", "Incident"]) == {1, 4, 5}
        assert await ids(allowed_orgs=["org-1"]) == {1}
        assert await ids(exclude_obj_id=1) == {4}

    async def test_env_isolation(self, db, index):
        await index.ensure_version(_MODEL, _DIM)
        vec = [1.0, 0.0, 0.0, 0.0]
        await index.upsert_chunks([_chunk(1, vec)], model=_MODEL, dim=_DIM)

        other_env = VectorIndex(db, env="staging")
        hits = await other_env.search(vec, classes=["UserRequest"], statuses=["resolved"], visibilities=["public"])

        assert hits == []


class TestDeleteAndStats:
    async def test_delete_object_removes_only_its_chunks(self, index):
        await index.ensure_version(_MODEL, _DIM)
        vec = [1.0, 0.0, 0.0, 0.0]
        await index.upsert_chunks(
            [
                _chunk(1, vec, chunk_kind="description"),
                _chunk(1, vec, chunk_kind="solution"),
                _chunk(2, vec),
            ],
            model=_MODEL,
            dim=_DIM,
        )

        deleted = await index.delete_object("UserRequest", 1)

        assert deleted == 2
        stats = await index.stats()
        assert stats is not None
        assert stats.rows == 1

    async def test_stats_reports_rows_and_size(self, index):
        await index.ensure_version(_MODEL, _DIM)
        await index.upsert_chunks([_chunk(1, [0.0] * _DIM)], model=_MODEL, dim=_DIM)

        stats = await index.stats()

        assert stats is not None
        assert stats.version == 1
        assert stats.rows == 1
        assert stats.size_bytes > 0
