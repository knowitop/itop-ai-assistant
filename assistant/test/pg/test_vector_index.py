"""VectorIndex integration tests: the SQL/pgvector seam against real Postgres."""

import asyncio
from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from vector.db import VectorDb
from vector.index import RECONCILE_SENTINEL, ChunkRecord, FingerprintMismatchError, VectorIndex

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
    filters: dict[str, str] | None = None,
) -> ChunkRecord:
    return ChunkRecord(
        obj_class=obj_class,
        obj_id=obj_id,
        chunk_kind=chunk_kind,
        chunk_n=chunk_n,
        visibility=visibility,
        status=status,
        org_id=org_id,
        content_hash=content_hash,
        embedding=embedding,
        created_at=_CREATED,
        filters=filters,
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
        assert "vector_chunk_v1_filters" in index_names

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

    async def test_filters_roundtrip_as_jsonb(self, index, db):
        await index.ensure_version(_MODEL, _DIM)
        chunk = _chunk(1, [0.0] * _DIM, filters={"service_id": "5"})

        await index.upsert_chunks([chunk], model=_MODEL, dim=_DIM)

        async with db.connect() as conn:
            row = (await conn.execute(text("SELECT filters FROM vector_chunk_v1 WHERE obj_id = 1"))).one()
        assert row.filters == {"service_id": "5"}


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


class TestChunkHashes:
    async def test_roundtrip_and_targeted_delete(self, index):
        await index.ensure_version(_MODEL, _DIM)
        vec = [0.0] * _DIM
        await index.upsert_chunks(
            [
                _chunk(1, vec, chunk_kind="body", chunk_n=0, content_hash="h0"),
                _chunk(1, vec, chunk_kind="body", chunk_n=1, content_hash="h1"),
                _chunk(1, vec, chunk_kind="solution", chunk_n=0, content_hash="h2"),
                _chunk(2, vec, content_hash="other"),
            ],
            model=_MODEL,
            dim=_DIM,
        )

        hashes = await index.get_chunk_hashes("UserRequest", 1)
        assert hashes == {("body", 0): "h0", ("body", 1): "h1", ("solution", 0): "h2"}

        deleted = await index.delete_chunks("UserRequest", 1, [("body", 1), ("solution", 0)])
        assert deleted == 2
        assert await index.get_chunk_hashes("UserRequest", 1) == {("body", 0): "h0"}
        assert await index.get_chunk_hashes("UserRequest", 2) == {("description", 0): "other"}

    async def test_no_version_is_empty(self, index):
        assert await index.get_chunk_hashes("UserRequest", 1) == {}
        assert await index.delete_chunks("UserRequest", 1, []) == 0

    async def test_list_object_ids_keyset(self, index):
        await index.ensure_version(_MODEL, _DIM)
        vec = [0.0] * _DIM
        await index.upsert_chunks(
            [_chunk(obj_id, vec, chunk_kind=kind) for obj_id in (5, 3, 9) for kind in ("body", "solution")],
            model=_MODEL,
            dim=_DIM,
        )

        assert await index.list_object_ids("UserRequest") == [3, 5, 9]  # distinct, ascending
        assert await index.list_object_ids("UserRequest", after=3, limit=1) == [5]
        assert await index.list_object_ids("UserRequest", after=9) == []
        assert await index.list_object_ids("Incident") == []


class TestCursors:
    async def test_roundtrip_and_upsert(self, index):
        assert await index.get_cursor("UserRequest") is None

        t1 = datetime(2026, 7, 1, 10, 0, tzinfo=UTC)
        t2 = datetime(2026, 7, 2, 10, 0, tzinfo=UTC)
        await index.set_cursor("UserRequest", t1)
        await index.set_cursor("UserRequest", t2)  # upsert, not a second row
        await index.set_cursor("Incident", t1)

        assert await index.get_cursor("UserRequest") == t2
        assert await index.list_cursors() == {"UserRequest": t2, "Incident": t1}

    async def test_sentinel_excluded_from_list(self, index):
        mark = datetime(2026, 7, 1, tzinfo=UTC)
        await index.set_cursor(RECONCILE_SENTINEL, mark)

        assert await index.list_cursors() == {}
        assert await index.get_cursor(RECONCILE_SENTINEL) == mark

    async def test_env_isolation_and_reset(self, db, index):
        other = VectorIndex(db, env="staging")
        t = datetime(2026, 7, 1, tzinfo=UTC)
        await index.set_cursor("UserRequest", t)
        await index.set_cursor(RECONCILE_SENTINEL, t)
        await other.set_cursor("UserRequest", t)

        assert await other.list_cursors() == {"UserRequest": t}

        await index.reset_cursors()

        assert await index.list_cursors() == {}
        assert await index.get_cursor(RECONCILE_SENTINEL) is None  # reset drops the mark too
        assert await other.list_cursors() == {"UserRequest": t}  # other env untouched


class TestJournal:
    async def test_start_finish_recent(self, index):
        first = await index.journal_start("sweep")
        second = await index.journal_start("reconcile")
        await index.journal_finish(first, status="ok", objects_seen=10, chunks_embedded=4, chunks_deleted=1)
        await index.journal_finish(second, status="error", error="boom")

        runs = await index.journal_recent(10)

        assert [run["id"] for run in runs] == [second, first]  # newest first
        by_id = {run["id"]: run for run in runs}
        assert by_id[first]["kind"] == "sweep"
        assert by_id[first]["status"] == "ok"
        assert by_id[first]["objects_seen"] == 10
        assert by_id[first]["chunks_embedded"] == 4
        assert by_id[first]["chunks_deleted"] == 1
        assert by_id[first]["finished_at"] is not None
        assert by_id[second]["status"] == "error"
        assert by_id[second]["error"] == "boom"

    async def test_recent_respects_limit_and_env(self, db, index):
        for _ in range(3):
            await index.journal_start("sweep")
        await VectorIndex(db, env="staging").journal_start("sweep")

        assert len(await index.journal_recent(2)) == 2
        assert all(run["kind"] == "sweep" for run in await index.journal_recent(10))
        assert len(await index.journal_recent(10)) == 3  # staging entry invisible


class TestAdvisoryLock:
    async def test_second_holder_gets_false_until_release(self, db, index):
        async with index.try_advisory_lock() as first:
            assert first is True
            async with VectorIndex(db, env="main").try_advisory_lock() as second:
                assert second is False

        async with index.try_advisory_lock() as again:
            assert again is True

    async def test_envs_do_not_contend(self, db, index):
        async with index.try_advisory_lock() as first:
            assert first is True
            async with VectorIndex(db, env="staging").try_advisory_lock() as other_env:
                assert other_env is True

    async def test_concurrent_sweeps_exclude_each_other(self, db, index):
        """Two tasks racing for the lock: exactly one wins."""
        results: list[bool] = []
        gate = asyncio.Event()

        async def contender():
            async with VectorIndex(db, env="main").try_advisory_lock() as locked:
                results.append(locked)
                await gate.wait()

        tasks = [asyncio.create_task(contender()) for _ in range(2)]
        while len(results) < 2:  # both are inside their context
            await asyncio.sleep(0.01)
        gate.set()
        await asyncio.gather(*tasks)

        assert sorted(results) == [False, True]
