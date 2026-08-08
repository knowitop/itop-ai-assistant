import asyncio
import unittest
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import fakeredis.aioredis
from fastapi.testclient import TestClient
from pydantic import SecretStr

from itop_ai_assistant.config import get_settings
from itop_ai_assistant.config_store import RedisConfigStore
from itop_ai_assistant.deps import AppDeps
from itop_ai_assistant.journal import RunJournal
from itop_ai_assistant.main import app
from itop_ai_assistant.prompt_store import PACKAGED_PROMPTS_DIR, FilePromptStore, RedisPromptStore
from itop_ai_assistant.state.ticket_state import TicketStateManager
from itop_ai_assistant.vector.index_journal import IndexJournal
from itop_ai_assistant.vector.indexer import SWEEP_TASK
from itop_ai_assistant.vector.qdrant_store import QdrantChunkStore
from itop_ai_assistant.vector.store import ChunkMetadata, ChunkRecord
from itop_ai_assistant.vector.sync_state import VectorSyncState

_NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)


def _chunk(obj_id: int, *, obj_class: str = "UserRequest", updated_at: datetime = _NOW) -> ChunkRecord:
    return ChunkRecord(
        meta=ChunkMetadata(
            obj_class=obj_class,
            obj_id=obj_id,
            chunk_kind="body",
            chunk_n=0,
            visibility="public",
            content_hash="hash",
            created_at=_NOW,
            filters={"status": "resolved"},
            updated_at=updated_at,
        ),
        embedding=[1.0, 0.0, 0.0, 0.0],
    )


class _FakeSource:
    """Stands in for `TicketVectorSource` — the real one needs a live iTop
    bundle from `deps.itop.get()`, which `_make_deps`'s `MagicMock()` can't
    provide."""

    name = "tickets"
    classes = ["UserRequest"]

    def __init__(self, existing: set[int]) -> None:
        self._existing = existing

    async def prepare(self) -> None:
        pass

    async def find_existing_ids(self, obj_class: str, ids: list[int]) -> set[int]:
        return self._existing & set(ids)


_BLANK = {
    "admin_token": None,
    "embeddings_base_url": None,
    "embeddings_model": None,
    "embeddings_api_key": None,
    "qdrant_url": None,
}


def _make_deps(redis, store_url: str | None = None, **settings_overrides) -> AppDeps:
    """`store_url` feeds the `ChunkStore` directly, not the production
    `qdrant_url` setting — these tests exercise `vector/router.py` against the
    `ChunkStore` port itself (configured-but-unreachable included)."""
    settings = get_settings().model_copy(update={**_BLANK, **settings_overrides})
    return AppDeps(
        settings=settings,
        itop=MagicMock(),
        state_manager=TicketStateManager(redis),
        config_store=RedisConfigStore(redis, settings),
        prompt_store=RedisPromptStore(FilePromptStore(PACKAGED_PROMPTS_DIR), redis),
        journal=RunJournal(redis),
        vector_store=QdrantChunkStore(store_url),
        vector_sync=VectorSyncState(redis),
        vector_journal=IndexJournal(redis),
    )


class VectorStatusTestCase(unittest.TestCase):
    def setUp(self):
        self.client = self.enterContext(TestClient(app))
        self.redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        self.client.app.state.deps = _make_deps(self.redis)
        # The lifespan built a real (empty) scheduler: no qdrant_url in the
        # test settings, so the sweep loop was never registered
        self.tasks = self.client.app.state.tasks


class TestVectorSources(VectorStatusTestCase):
    """GET /api/vector/sources — the chunking vocabulary the admin UI renders
    its editor from (ADR-018). Shares the status harness on purpose: both
    endpoints answer from the same registry."""

    def test_declares_the_ticket_source_vocabulary(self):
        # Nothing is configured in this harness — no Qdrant, no embeddings,
        # indexing off. The editor must still be usable.
        body = self.client.get("/api/vector/sources").json()

        [source] = body["sources"]
        self.assertEqual(source["name"], "tickets")
        self.assertEqual(source["classes"], ["UserRequest", "Incident"])
        self.assertIn("title", source["fields"])
        by_kind = {f["kind"]: f for f in source["fragments"]}
        self.assertEqual(by_kind["body"], {"kind": "body", "visibility": "public", "optional": False})
        self.assertEqual(by_kind["log:private"], {"kind": "log:private", "visibility": "internal", "optional": True})

    def test_classes_follow_the_saved_config(self):
        self.client.patch("/api/setup/vector", json={"classes": {"Problem": {"index_values": []}}})

        body = self.client.get("/api/vector/sources").json()

        self.assertEqual(body["sources"][0]["classes"], ["Problem"])

    def test_no_itop_call_is_made(self):
        # Reading a declaration must not touch iTop: `prepare()` is what needs
        # a live bundle, and this endpoint never calls it.
        self.client.get("/api/vector/sources")

        self.client.app.state.deps.itop.get.assert_not_called()


class TestVectorStatus(VectorStatusTestCase):
    def test_unconfigured_database(self):
        body = self.client.get("/api/vector/status").json()

        self.assertFalse(body["enabled"])
        self.assertFalse(body["embeddings_configured"])
        self.assertFalse(body["store"]["configured"])
        self.assertIsNone(body["store"]["ok"])
        self.assertIsNone(body["index"])
        self.assertIsNone(body["sync"])
        self.assertIsNone(body["last_reconcile"])
        self.assertEqual(body["runs"], [])
        self.assertFalse(body["indexer_running"])

    def test_database_down_reports_error_not_500(self):
        # Port 1 is never listening — connection fails fast
        self.client.app.state.deps = _make_deps(self.redis, store_url="http://localhost:1")

        response = self.client.get("/api/vector/status")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["store"]["configured"])
        self.assertFalse(body["store"]["ok"])
        self.assertTrue(body["store"]["error"])
        self.assertIsNone(body["index"])

    def test_embeddings_configured_flag(self):
        self.client.app.state.deps = _make_deps(
            self.redis, embeddings_base_url="http://emb/v1", embeddings_model="bge-m3"
        )

        body = self.client.get("/api/vector/status").json()

        self.assertTrue(body["embeddings_configured"])

    def test_enabled_reflects_vector_section(self):
        self.client.patch("/api/setup/vector", json={"enabled": True})

        body = self.client.get("/api/vector/status").json()

        self.assertTrue(body["enabled"])

    def test_index_is_a_list_of_families(self):
        # `tickets` is always in the registry (registry.py), so it appears
        # `configured: true` even with no active version yet — same "no
        # index" case the old single-block response used to report.
        deps = _make_deps(self.redis, store_url=":memory:")
        self.client.app.state.deps = deps

        body = self.client.get("/api/vector/status").json()

        self.assertEqual(len(body["index"]), 1)
        entry = body["index"][0]
        self.assertEqual(entry["family"], "tickets")
        self.assertTrue(entry["configured"])
        self.assertIsNone(entry["active_version"])
        self.assertIsNone(entry["rows"])

    def test_a_family_no_longer_in_the_registry_is_reported_as_unconfigured(self):
        # list_families() (Qdrant) knows about `kb_articles`; build_vector_sources()
        # (code) does not — the union still surfaces it, flagged instead of dropped.
        deps = _make_deps(self.redis, store_url=":memory:")
        asyncio.run(deps.vector_store.ensure_version("kb_articles", "bge-m3", 4))
        self.client.app.state.deps = deps

        body = self.client.get("/api/vector/status").json()

        families = {entry["family"]: entry for entry in body["index"]}
        self.assertEqual(set(families), {"tickets", "kb_articles"})
        self.assertTrue(families["tickets"]["configured"])
        self.assertFalse(families["kb_articles"]["configured"])
        self.assertEqual(families["kb_articles"]["active_version"], 1)
        self.assertEqual(families["kb_articles"]["model"], "bge-m3")
        self.assertEqual(families["kb_articles"]["rows"], 0)

    def test_requires_admin_token_when_set(self):
        self.client.app.state.deps = _make_deps(self.redis, admin_token=SecretStr("s3cret"))

        self.assertEqual(self.client.get("/api/vector/status").status_code, 401)
        response = self.client.get("/api/vector/status", headers={"Authorization": "Bearer s3cret"})
        self.assertEqual(response.status_code, 200)


class TestReindex(VectorStatusTestCase):
    def test_409_when_database_not_configured(self):
        self.client.patch("/api/setup/vector", json={"enabled": True})

        response = self.client.post("/api/vector/reindex")

        self.assertEqual(response.status_code, 409)
        self.assertIn("qdrant_url", response.json()["detail"])

    def test_409_when_vector_disabled(self):
        self.client.app.state.deps = _make_deps(self.redis, store_url="http://localhost:1")

        response = self.client.post("/api/vector/reindex")

        self.assertEqual(response.status_code, 409)
        self.assertIn("disabled", response.json()["detail"])

    def test_503_when_the_request_cannot_be_stored(self):
        """The mark is a flag in Redis now, so an unreachable Redis is a real
        failure — it must say so instead of pretending the backfill was
        scheduled."""
        deps = _make_deps(self.redis, store_url="http://localhost:1")
        deps.vector_sync.request_reindex = AsyncMock(side_effect=ConnectionError("redis down"))
        self.client.app.state.deps = deps
        self.client.patch("/api/setup/vector", json={"enabled": True})

        response = self.client.post("/api/vector/reindex")

        self.assertEqual(response.status_code, 503)
        self.assertIn("unavailable", response.json()["detail"])

    def test_202_marks_the_request_and_wakes_the_sweep(self):
        """The request is a flag in Redis — the wake-up only makes the local
        loop act on it sooner."""
        deps = _make_deps(self.redis, store_url="http://localhost:1")
        self.client.app.state.deps = deps
        self.client.patch("/api/setup/vector", json={"enabled": True})
        self.tasks.wake = MagicMock(return_value=True)

        response = self.client.post("/api/vector/reindex")

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json(), {"status": "scheduled"})
        self.assertTrue(asyncio.run(deps.vector_sync.reindex_pending()))
        self.tasks.wake.assert_called_once_with(SWEEP_TASK)


class TestSweep(VectorStatusTestCase):
    """POST /api/vector/sweep — the ordinary incremental pass, on demand.
    Nothing is written anywhere: waking the local loop is the whole action,
    which is why every case where the tick could not do real work is a
    refusal rather than a 202."""

    def _configured_deps(self) -> AppDeps:
        deps = _make_deps(
            self.redis, store_url="http://localhost:1", embeddings_base_url="http://emb/v1", embeddings_model="bge-m3"
        )
        self.client.app.state.deps = deps
        self.client.patch("/api/setup/vector", json={"enabled": True})
        return deps

    def test_409_when_database_not_configured(self):
        self.client.patch("/api/setup/vector", json={"enabled": True})

        response = self.client.post("/api/vector/sweep")

        self.assertEqual(response.status_code, 409)
        self.assertIn("qdrant_url", response.json()["detail"])

    def test_409_when_vector_disabled(self):
        self.client.app.state.deps = _make_deps(self.redis, store_url="http://localhost:1")

        response = self.client.post("/api/vector/sweep")

        self.assertEqual(response.status_code, 409)
        self.assertIn("disabled", response.json()["detail"])

    def test_409_when_embeddings_not_configured(self):
        self.client.app.state.deps = _make_deps(self.redis, store_url="http://localhost:1")
        self.client.patch("/api/setup/vector", json={"enabled": True})
        self.tasks.wake = MagicMock(return_value=True)

        response = self.client.post("/api/vector/sweep")

        self.assertEqual(response.status_code, 409)
        self.assertIn("Embeddings", response.json()["detail"])
        self.tasks.wake.assert_not_called()

    def test_409_when_this_process_has_no_sweep_loop(self):
        """The loop is registered at startup from the settings, not from the
        deps a request happens to see — an unregistered task means the wake-up
        went nowhere, and saying "scheduled" would be a lie."""
        self._configured_deps()

        response = self.client.post("/api/vector/sweep")

        self.assertEqual(response.status_code, 409)
        self.assertIn("restart", response.json()["detail"])

    def test_202_wakes_the_sweep_without_requesting_a_backfill(self):
        deps = self._configured_deps()
        self.tasks.wake = MagicMock(return_value=True)

        response = self.client.post("/api/vector/sweep")

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json(), {"status": "scheduled"})
        self.tasks.wake.assert_called_once_with(SWEEP_TASK)
        # The cursors stay where they are — that is the whole difference
        self.assertFalse(asyncio.run(deps.vector_sync.reindex_pending()))

    def test_requires_admin_token_when_set(self):
        self.client.app.state.deps = _make_deps(self.redis, admin_token=SecretStr("s3cret"))

        self.assertEqual(self.client.post("/api/vector/sweep").status_code, 401)


class TestSearch(VectorStatusTestCase):
    def test_409_when_database_not_configured(self):
        self.client.patch("/api/setup/vector", json={"enabled": True})

        response = self.client.post("/api/vector/search", json={"family": "tickets", "text": "printer"})

        self.assertEqual(response.status_code, 409)
        self.assertIn("qdrant_url", response.json()["detail"])

    def test_409_when_vector_disabled(self):
        self.client.app.state.deps = _make_deps(self.redis, store_url=":memory:")

        response = self.client.post("/api/vector/search", json={"family": "tickets", "text": "printer"})

        self.assertEqual(response.status_code, 409)
        self.assertIn("disabled", response.json()["detail"])

    def test_409_when_embeddings_not_configured(self):
        self.client.app.state.deps = _make_deps(self.redis, store_url=":memory:")
        self.client.patch("/api/setup/vector", json={"enabled": True})

        response = self.client.post("/api/vector/search", json={"family": "tickets", "text": "printer"})

        self.assertEqual(response.status_code, 409)
        self.assertIn("Embeddings", response.json()["detail"])

    def test_404_for_an_unknown_family(self):
        self.client.app.state.deps = _make_deps(
            self.redis, store_url=":memory:", embeddings_base_url="http://emb/v1", embeddings_model="bge-m3"
        )
        self.client.patch("/api/setup/vector", json={"enabled": True})

        response = self.client.post("/api/vector/search", json={"family": "kb_articles", "text": "printer"})

        self.assertEqual(response.status_code, 404)
        self.assertIn("kb_articles", response.json()["detail"])

    def test_no_active_index_answers_empty_without_asking_the_source(self):
        deps = _make_deps(
            self.redis, store_url=":memory:", embeddings_base_url="http://emb/v1", embeddings_model="bge-m3"
        )
        self.client.app.state.deps = deps
        self.client.patch("/api/setup/vector", json={"enabled": True})
        embedder = MagicMock()
        embedder.embed = AsyncMock(return_value=[[1.0, 0.0, 0.0, 0.0]])
        embedder.aclose = AsyncMock()
        source = _FakeSource(existing=set())
        source.find_existing_ids = AsyncMock(side_effect=source.find_existing_ids)

        with (
            patch("itop_ai_assistant.vector.router.EmbeddingsClient", return_value=embedder),
            patch("itop_ai_assistant.vector.router.build_vector_sources", return_value=[source]),
        ):
            response = self.client.post("/api/vector/search", json={"family": "tickets", "text": "printer"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["hits"], [])
        embedder.aclose.assert_awaited_once()
        source.find_existing_ids.assert_not_awaited()

    def test_returns_hits_confirmed_by_the_source(self):
        deps = _make_deps(
            self.redis, store_url=":memory:", embeddings_base_url="http://emb/v1", embeddings_model="bge-m3"
        )
        self.client.app.state.deps = deps
        self.client.patch("/api/setup/vector", json={"enabled": True})

        async def _seed() -> None:
            await deps.vector_store.ensure_version("tickets", "bge-m3", 4)
            await deps.vector_store.upsert_chunks(
                [_chunk(1), _chunk(2), _chunk(3)], family="tickets", model="bge-m3", dim=4
            )

        asyncio.run(_seed())
        embedder = MagicMock()
        embedder.embed = AsyncMock(return_value=[[1.0, 0.0, 0.0, 0.0]])
        embedder.aclose = AsyncMock()
        # The source only confirms 1 and 3 — 2 must be dropped from the answer
        source = _FakeSource(existing={1, 3})

        with (
            patch("itop_ai_assistant.vector.router.EmbeddingsClient", return_value=embedder),
            patch("itop_ai_assistant.vector.router.build_vector_sources", return_value=[source]),
        ):
            response = self.client.post(
                "/api/vector/search",
                json={
                    "family": "tickets",
                    "text": "printer is on fire",
                    "classes": ["UserRequest"],
                    "filters": {"status": ["resolved"]},
                    "top": 5,
                },
            )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        # Order isn't asserted here — all three chunks share one embedding, so
        # score ties leave ranking unspecified; that ordering is `SimilarSearch`'s
        # own concern, covered by test_vector_search.py.
        self.assertEqual({h["obj_id"] for h in body["hits"]}, {1, 3})
        self.assertEqual(body["stats"], {"requested": 15, "found": 3, "dropped_by_resolve": 1})
        self.assertIsNone(body["allowed_org_ids"])
        embedder.embed.assert_awaited_once_with(["printer is on fire"])
        embedder.aclose.assert_awaited_once()

    def test_a_date_window_narrows_the_answer(self):
        deps = _make_deps(
            self.redis, store_url=":memory:", embeddings_base_url="http://emb/v1", embeddings_model="bge-m3"
        )
        self.client.app.state.deps = deps
        self.client.patch("/api/setup/vector", json={"enabled": True})

        async def _seed() -> None:
            await deps.vector_store.ensure_version("tickets", "bge-m3", 4)
            await deps.vector_store.upsert_chunks(
                [_chunk(1), _chunk(2, updated_at=datetime(2020, 1, 1, tzinfo=UTC))],
                family="tickets",
                model="bge-m3",
                dim=4,
            )

        asyncio.run(_seed())
        embedder = MagicMock()
        embedder.embed = AsyncMock(return_value=[[1.0, 0.0, 0.0, 0.0]])
        embedder.aclose = AsyncMock()

        with (
            patch("itop_ai_assistant.vector.router.EmbeddingsClient", return_value=embedder),
            patch("itop_ai_assistant.vector.router.build_vector_sources", return_value=[_FakeSource(existing={1, 2})]),
        ):
            response = self.client.post(
                "/api/vector/search",
                json={"family": "tickets", "text": "printer", "updated": {"after": "2026-01-01T00:00:00Z"}},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual([h["obj_id"] for h in response.json()["hits"]], [1])

    def test_an_impossible_window_is_rejected_on_parsing(self):
        # 422 from the body validator, not a 500 out of the store: the domain
        # `DateRange` invariants are checked before the search runs at all
        self.client.app.state.deps = _make_deps(
            self.redis, store_url=":memory:", embeddings_base_url="http://emb/v1", embeddings_model="bge-m3"
        )
        self.client.patch("/api/setup/vector", json={"enabled": True})

        for window in ({}, {"after": "2026-08-01T00:00:00Z", "before": "2020-01-01T00:00:00Z"}):
            with self.subTest(window=window):
                response = self.client.post(
                    "/api/vector/search", json={"family": "tickets", "text": "printer", "updated": window}
                )

                self.assertEqual(response.status_code, 422)

    def test_requires_admin_token_when_set(self):
        self.client.app.state.deps = _make_deps(self.redis, admin_token=SecretStr("s3cret"))

        response = self.client.post("/api/vector/search", json={"family": "tickets", "text": "printer"})

        self.assertEqual(response.status_code, 401)


class TestSearchAsPrincipal(VectorStatusTestCase):
    """TASK-015: `principal_token` resolves and org-prefilters under a given
    iTop identity instead of the service account — the only live-testable
    path for R4 before there is a console to call it in production."""

    def _seed_and_patch(self, deps, *, existing, allowed_org_ids):
        async def _seed() -> None:
            await deps.vector_store.ensure_version("tickets", "bge-m3", 4)
            await deps.vector_store.upsert_chunks([_chunk(1), _chunk(2)], family="tickets", model="bge-m3", dim=4)

        asyncio.run(_seed())
        embedder = MagicMock()
        embedder.embed = AsyncMock(return_value=[[1.0, 0.0, 0.0, 0.0]])
        embedder.aclose = AsyncMock()
        source = _FakeSource(existing=set())  # must not be asked — see assertion below
        source.find_existing_ids = AsyncMock(side_effect=source.find_existing_ids)
        bundle = MagicMock()
        bundle.ticket_repo.find_existing_ids = AsyncMock(side_effect=lambda _cls, ids: existing & set(ids))
        bundle.access_repo.allowed_org_ids = AsyncMock(return_value=allowed_org_ids)
        deps.itop.for_principal = AsyncMock(return_value=bundle)
        return embedder, source, bundle

    def test_resolves_under_the_given_principal_not_the_service_account(self):
        deps = _make_deps(
            self.redis, store_url=":memory:", embeddings_base_url="http://emb/v1", embeddings_model="bge-m3"
        )
        self.client.app.state.deps = deps
        self.client.patch("/api/setup/vector", json={"enabled": True})
        # Unrestricted org scope here — the seeded chunks carry no `org_id`
        # filter value, so an org pre-filter would (correctly) match nothing;
        # that interaction is covered separately below.
        embedder, source, bundle = self._seed_and_patch(deps, existing={1}, allowed_org_ids=None)

        with (
            patch("itop_ai_assistant.vector.router.EmbeddingsClient", return_value=embedder),
            patch("itop_ai_assistant.vector.router.build_vector_sources", return_value=[source]),
        ):
            response = self.client.post(
                "/api/vector/search",
                json={"family": "tickets", "text": "printer", "principal_token": "engineer-token"},
            )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual({h["obj_id"] for h in body["hits"]}, {1})
        self.assertIsNone(body["allowed_org_ids"])
        source.find_existing_ids.assert_not_awaited()
        bundle.ticket_repo.find_existing_ids.assert_awaited()
        deps.itop.for_principal.assert_awaited_once()
        principal = deps.itop.for_principal.await_args.args[0]
        self.assertEqual(principal.auth.token, "engineer-token")

    def test_allowed_org_ids_fill_the_org_filter_by_default(self):
        deps = _make_deps(
            self.redis, store_url=":memory:", embeddings_base_url="http://emb/v1", embeddings_model="bge-m3"
        )
        self.client.app.state.deps = deps
        self.client.patch("/api/setup/vector", json={"enabled": True})
        embedder, source, _ = self._seed_and_patch(deps, existing={1, 2}, allowed_org_ids=["3", "9"])
        store_search = AsyncMock(wraps=deps.vector_store.search)
        with (
            patch("itop_ai_assistant.vector.router.EmbeddingsClient", return_value=embedder),
            patch("itop_ai_assistant.vector.router.build_vector_sources", return_value=[source]),
            patch.object(deps.vector_store, "search", store_search),
        ):
            self.client.post(
                "/api/vector/search",
                json={"family": "tickets", "text": "printer", "principal_token": "engineer-token"},
            )

        self.assertEqual(store_search.await_args.kwargs["filters"], {"org_id": ["3", "9"]})

    def test_an_explicit_org_filter_is_not_overridden(self):
        deps = _make_deps(
            self.redis, store_url=":memory:", embeddings_base_url="http://emb/v1", embeddings_model="bge-m3"
        )
        self.client.app.state.deps = deps
        self.client.patch("/api/setup/vector", json={"enabled": True})
        embedder, source, _ = self._seed_and_patch(deps, existing={1, 2}, allowed_org_ids=["3"])
        store_search = AsyncMock(wraps=deps.vector_store.search)
        with (
            patch("itop_ai_assistant.vector.router.EmbeddingsClient", return_value=embedder),
            patch("itop_ai_assistant.vector.router.build_vector_sources", return_value=[source]),
            patch.object(deps.vector_store, "search", store_search),
        ):
            self.client.post(
                "/api/vector/search",
                json={
                    "family": "tickets",
                    "text": "printer",
                    "principal_token": "engineer-token",
                    "filters": {"org_id": ["7"]},
                },
            )

        self.assertEqual(store_search.await_args.kwargs["filters"], {"org_id": ["7"]})

    def test_unrestricted_principal_adds_no_org_filter(self):
        deps = _make_deps(
            self.redis, store_url=":memory:", embeddings_base_url="http://emb/v1", embeddings_model="bge-m3"
        )
        self.client.app.state.deps = deps
        self.client.patch("/api/setup/vector", json={"enabled": True})
        embedder, source, _ = self._seed_and_patch(deps, existing={1, 2}, allowed_org_ids=None)
        store_search = AsyncMock(wraps=deps.vector_store.search)
        with (
            patch("itop_ai_assistant.vector.router.EmbeddingsClient", return_value=embedder),
            patch("itop_ai_assistant.vector.router.build_vector_sources", return_value=[source]),
            patch.object(deps.vector_store, "search", store_search),
        ):
            response = self.client.post(
                "/api/vector/search",
                json={"family": "tickets", "text": "printer", "principal_token": "engineer-token"},
            )

        self.assertIsNone(store_search.await_args.kwargs["filters"])
        self.assertIsNone(response.json()["allowed_org_ids"])

    def test_other_families_are_not_supported_yet(self):
        deps = _make_deps(
            self.redis, store_url=":memory:", embeddings_base_url="http://emb/v1", embeddings_model="bge-m3"
        )
        self.client.app.state.deps = deps
        self.client.patch("/api/setup/vector", json={"enabled": True})
        source = MagicMock()
        source.name = "kb_articles"
        source.prepare = AsyncMock()

        with patch("itop_ai_assistant.vector.router.build_vector_sources", return_value=[source]):
            response = self.client.post(
                "/api/vector/search",
                json={"family": "kb_articles", "text": "printer", "principal_token": "engineer-token"},
            )

        self.assertEqual(response.status_code, 501)


if __name__ == "__main__":
    unittest.main()
