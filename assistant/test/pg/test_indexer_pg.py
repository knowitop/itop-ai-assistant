"""End-to-end sweep against real Postgres: fake iTop repositories and a fake
embedder, everything below `VectorIndex` is real (tables, upserts, cursors,
advisory lock, journal)."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import text

from config import EmbeddingsConfig, VectorConfig
from domain.ticket import Ticket
from vector.db import VectorDb
from vector.indexer import VectorIndexer

_DIM = 4
_NOW = datetime(2026, 7, 10, 12, 0, tzinfo=UTC)

_VECTOR_CFG = VectorConfig(
    enabled=True,
    classes=["UserRequest"],
    profiles={"UserRequest": {"body": ["description"]}},
    max_chunk_tokens=10,  # 30-char budget → long descriptions split into chunks
    sweep_throttle_seconds=0,
    index_statuses=["resolved", "closed"],
)
_EMB_CFG = EmbeddingsConfig(base_url="http://fake/v1", model="test-model", dimension=_DIM)


class FakeEmbedder:
    def __init__(self, *args, **kwargs):
        self.calls: list[int] = []

    async def embed(self, texts):
        self.calls.append(len(texts))
        return [[0.5] * _DIM for _ in texts]

    async def aclose(self):
        pass


class FakeTicketRepo:
    def __init__(self, tickets: list[Ticket]):
        self.tickets = tickets

    async def find_modified_since(self, obj_class, since, *, page, page_size):
        rows = [t for t in self.tickets if t.obj_class == obj_class]
        start = (int(page) - 1) * page_size
        return rows[start : start + page_size]

    async def find_existing_ids(self, obj_class, ids):
        alive = {int(t.id) for t in self.tickets if t.obj_class == obj_class}
        return alive & set(ids)


def _ticket(obj_id: int, description: str, *, status: str = "resolved") -> Ticket:
    return Ticket(
        obj_class="UserRequest",
        id=str(obj_id),
        description=description,
        status=status,
        last_update=_NOW,
        created_at=_NOW,
    )


def _deps(db: VectorDb, tickets: list[Ticket]) -> MagicMock:
    """`itop.get()` is mocked, but the sweep still goes through the real
    `TicketVectorSource` (via `build_vector_sources`) — this test exercises
    the source seam, not just `VectorIndex`."""
    deps = MagicMock()
    deps.vector_db = db
    deps.config_store.get = AsyncMock(
        side_effect=lambda name, model: {"vector": _VECTOR_CFG, "embeddings": _EMB_CFG}[name]
    )
    bundle = MagicMock()
    bundle.ticket_repo = FakeTicketRepo(tickets)
    deps.itop.get = AsyncMock(return_value=bundle)
    return deps


@pytest.fixture
async def db(migrated: str, engine):
    vdb = VectorDb(migrated)
    yield vdb
    await vdb.aclose()


async def _sweep(deps) -> tuple:
    embedder = FakeEmbedder()
    with patch("vector.indexer.EmbeddingsClient", return_value=embedder):
        report = await VectorIndexer(deps).sweep_once()
    return report, embedder


async def _rows(engine) -> list:
    async with engine.connect() as conn:
        result = await conn.execute(
            text("SELECT obj_id, chunk_kind, chunk_n, status FROM vector_chunk_v1 ORDER BY obj_id, chunk_kind, chunk_n")
        )
        return list(result)


class TestSweepEndToEnd:
    async def test_sweep_shrink_and_noop(self, db, engine):
        long_description = "First sentence here. Second sentence goes on."  # > 30 chars → 2 chunks
        tickets = [_ticket(1, long_description), _ticket(2, "Short.")]
        deps = _deps(db, tickets)

        # --- first sweep: real rows land in Postgres
        report, embedder = await _sweep(deps)

        assert report.status == "ok"
        assert report.kind == "sweep"
        assert report.objects_seen == 2
        assert report.chunks_embedded == 3
        assert embedder.calls == [3]  # one embed call for the whole page
        assert [(r.obj_id, r.chunk_kind, r.chunk_n) for r in await _rows(engine)] == [
            (1, "body", 0),
            (1, "body", 1),
            (2, "body", 0),
        ]

        # cursor advanced, journal has ok runs (sweep + first reconcile)
        from vector.index import VectorIndex

        index = VectorIndex(db, env="main")
        assert await index.get_cursor("UserRequest") == _NOW
        runs = await index.journal_recent(10)
        assert {run["kind"] for run in runs} == {"sweep", "reconcile"}
        assert all(run["status"] == "ok" for run in runs)

        # --- shrink: shorter description → fewer chunks, extra one deleted
        tickets[0] = _ticket(1, "Tiny.")
        report, embedder = await _sweep(deps)

        assert report.status == "ok"
        assert report.chunks_embedded == 1  # only the changed chunk re-embedded
        assert report.chunks_deleted == 1  # (body, 1) vanished
        assert [(r.obj_id, r.chunk_n) for r in await _rows(engine)] == [(1, 0), (2, 0)]

        # --- no changes: second sweep is a no-op, nothing embedded
        report, embedder = await _sweep(deps)

        assert report.status == "ok"
        assert report.chunks_embedded == 0
        assert report.chunks_deleted == 0
        assert embedder.calls == []

    async def test_reopened_ticket_chunks_deleted(self, db, engine):
        tickets = [_ticket(1, "Something broke.")]
        deps = _deps(db, tickets)
        await _sweep(deps)
        assert len(await _rows(engine)) == 1

        tickets[0] = _ticket(1, "Something broke.", status="assigned")
        report, _ = await _sweep(deps)

        assert report.status == "ok"
        assert report.chunks_deleted == 1
        assert await _rows(engine) == []
