"""The read path: candidates from the index, confirmed by the source."""

import unittest
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import fakeredis

from itop_ai_assistant.config import EmbeddingsConfig
from itop_ai_assistant.core.principal import Principal
from itop_ai_assistant.state.counters import Counter, DailyCounters
from itop_ai_assistant.vector.config import FamilyConfig, VectorConfig
from itop_ai_assistant.vector.ports.query import FindStats, ObjectHit, SearchQuery
from itop_ai_assistant.vector.ports.store import DateRange, IndexMeta, SearchHit
from itop_ai_assistant.vector.use_cases.search import SearchUnavailable, SimilarSearch, UnknownFamily

_NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
_FAMILY = "tickets"
_ENGINEER = Principal.delegated("tok", login="ivanov", name="Ivan Ivanov")


def _family_switched_off() -> VectorConfig:
    """Indexing on for the deployment, off for `_FAMILY` — the shape the
    per-family gate exists for."""
    return VectorConfig(enabled=True, families={_FAMILY: FamilyConfig(enabled=False)})


def _built_by(model: str, dim: int = 1024) -> IndexMeta:
    """An index version with a fingerprint of its own — `bge-m3`/1024 is what
    `_FakeConfigStore` configures, so anything else reads as a rebuild."""
    return IndexMeta(family=_FAMILY, version=1, model=model, dim=dim)


def _hit(obj_id: int, score: float, obj_class: str = "UserRequest") -> SearchHit:
    return SearchHit(obj_class=obj_class, obj_id=obj_id, score=score)


def _query(**overrides: Any) -> SearchQuery:
    """A plausible scenario, with only what a test cares about spelled out."""
    return SearchQuery(
        **{
            "text": "q",
            "family": _FAMILY,
            "classes": ["UserRequest"],
            "filters": {"status": ["resolved"]},
            **overrides,
        }
    )


class _FakeSource:
    """A `VectorSource` cut down to what the read path touches, recording who
    it was asked for — the search's own contract-in (TASK-032)."""

    def __init__(self, name: str, *, existing: set[int] | None = None) -> None:
        self.name = name
        self._existing = existing
        self.asked: list[tuple[Principal, str, list[int]]] = []

    async def confirm_visible(self, principal: Principal, obj_class: str, ids: list[int]) -> set[int]:
        self.asked.append((principal, obj_class, sorted(ids)))
        return set(ids) if self._existing is None else self._existing & set(ids)


class _FakeConfigStore:
    """`vector`/`embeddings` sections, both defaulting to "search can run" so
    a test that does not care about availability does not have to spell it
    out — only `TestAvailability`/`TestSearchUnavailable` below override one."""

    def __init__(self, *, vector: VectorConfig | None = None, embeddings: EmbeddingsConfig | None = None) -> None:
        self.vector = vector if vector is not None else VectorConfig(enabled=True)
        self.embeddings = (
            embeddings if embeddings is not None else EmbeddingsConfig(base_url="http://emb/v1", model="bge-m3")
        )

    async def get(self, module: str, model: type) -> Any:
        if module == "vector":
            return self.vector
        if module == "embeddings":
            return self.embeddings
        raise AssertionError(f"unexpected config section {module!r}")

    def defaults(self, module: str, model: type) -> Any:
        raise NotImplementedError

    async def set(self, module: str, values: dict, model: type) -> Any:
        raise NotImplementedError

    async def reset(self, module: str, fields: Sequence[str] | None = None) -> None:
        raise NotImplementedError


class _SearchTestCase(unittest.IsolatedAsyncioTestCase):
    """Base for every `SimilarSearch` test: builds the door with a fake store,
    a fake config section pair and an `EmbeddingsClient` patched for the
    length of the test (TASK-033 — the door owns the client now, so a test
    can no longer just hand one to the constructor)."""

    def _search(
        self,
        hits: list[SearchHit],
        *,
        existing: set[int] | None = None,
        families: tuple[str, ...] = (_FAMILY,),
        store_configured: bool = True,
        vector_cfg: VectorConfig | None = None,
        embeddings_cfg: EmbeddingsConfig | None = None,
        active_meta: IndexMeta | None = None,
    ) -> tuple[SimilarSearch, MagicMock, MagicMock, _FakeSource]:
        store = MagicMock()
        store.configured = store_configured
        store.search = AsyncMock(return_value=hits)
        # `None` — a family with no index version yet — is the shape that has
        # no fingerprint to disagree with, so the model gate stays open and a
        # test not about that gate needs to say nothing about it.
        store.active_meta = AsyncMock(return_value=active_meta)
        embedder = MagicMock()
        embedder.embed = AsyncMock(return_value=[[1.0, 0.0]])
        embedder.aclose = AsyncMock()
        patcher = patch("itop_ai_assistant.vector.use_cases.search.EmbeddingsClient", return_value=embedder)
        self.embedder_cls = patcher.start()
        self.addCleanup(patcher.stop)
        sources = [_FakeSource(name, existing=existing) for name in families]
        config = _FakeConfigStore(vector=vector_cfg, embeddings=embeddings_cfg)
        # `build_sources` goes unused once `sources` is injected — same seam
        # as `VectorIndexer`'s, and for the same reason.
        self.counters = DailyCounters(fakeredis.aioredis.FakeRedis(decode_responses=True))
        search = SimilarSearch(store, config, MagicMock(), self.counters, sources=sources)
        return search, store, embedder, sources[0]


class TestSimilarSearch(_SearchTestCase):
    async def test_returns_hits_best_first(self):
        search, _, _, _ = self._search([_hit(1, 0.9), _hit(2, 0.7)])

        result = await search.find(_query(text="printer is dead"), _ENGINEER)

        self.assertEqual(result.hits, [ObjectHit("UserRequest", 1, 0.9), ObjectHit("UserRequest", 2, 0.7)])

    async def test_the_query_is_embedded_once(self):
        search, _, embedder, _ = self._search([_hit(1, 0.9)])

        await search.find(_query(text="printer is dead"), _ENGINEER)

        embedder.embed.assert_awaited_once_with(["printer is dead"])

    async def test_filters_reach_the_store(self):
        search, store, _, _ = self._search([])

        await search.find(
            _query(
                classes=["UserRequest", "Incident"],
                filters={"status": ["resolved", "closed"]},
                exclude=("UserRequest", 7),
                updated=DateRange(after=_NOW),
                candidates=15,
            ),
            _ENGINEER,
        )

        kwargs = store.search.await_args.kwargs
        self.assertEqual(kwargs["family"], _FAMILY)
        self.assertEqual(kwargs["classes"], ["UserRequest", "Incident"])
        self.assertEqual(kwargs["filters"], {"status": ["resolved", "closed"]})
        self.assertEqual(kwargs["visibilities"], ["public", "internal"])
        self.assertEqual(kwargs["exclude"], ("UserRequest", 7))
        self.assertEqual(kwargs["updated"], DateRange(after=_NOW))
        self.assertEqual(kwargs["limit"], 15)

    async def test_both_date_windows_reach_the_store_under_the_same_names(self):
        search, store, _, _ = self._search([])
        created = DateRange(after=datetime(2020, 1, 1, tzinfo=UTC), before=_NOW)

        await search.find(_query(created=created, updated=DateRange(after=_NOW)), _ENGINEER)

        kwargs = store.search.await_args.kwargs
        self.assertEqual(kwargs["created"], created)
        self.assertEqual(kwargs["updated"], DateRange(after=_NOW))

    async def test_date_windows_default_to_none(self):
        search, store, _, _ = self._search([_hit(1, 0.9)])

        await search.find(_query(), _ENGINEER)

        self.assertIsNone(store.search.await_args.kwargs["created"])
        self.assertIsNone(store.search.await_args.kwargs["updated"])

    async def test_chunk_kinds_reaches_the_store_under_the_same_name(self):
        search, store, _, _ = self._search([])

        await search.find(_query(chunk_kinds=["profile", "body"]), _ENGINEER)

        self.assertEqual(store.search.await_args.kwargs["chunk_kinds"], ["profile", "body"])

    async def test_chunk_kinds_defaults_to_none(self):
        search, store, _, _ = self._search([_hit(1, 0.9)])

        await search.find(_query(), _ENGINEER)

        self.assertIsNone(store.search.await_args.kwargs["chunk_kinds"])

    async def test_min_score_reaches_the_store_as_score_threshold(self):
        search, store, _, _ = self._search([])

        await search.find(_query(min_score=0.5), _ENGINEER)

        self.assertEqual(store.search.await_args.kwargs["score_threshold"], 0.5)

    async def test_min_score_defaults_to_no_floor(self):
        search, store, _, _ = self._search([_hit(1, 0.9)])

        await search.find(_query(), _ENGINEER)

        self.assertIsNone(store.search.await_args.kwargs["score_threshold"])

    async def test_candidates_the_source_no_longer_returns_are_dropped(self):
        # The pre-filter is deliberately over-permissive (ADR-003); the source
        # is the authority on what may be quoted
        search, _, _, _ = self._search([_hit(1, 0.9), _hit(2, 0.8), _hit(3, 0.7)], existing={1, 3})

        result = await search.find(_query(), _ENGINEER)

        self.assertEqual([hit.obj_id for hit in result.hits], [1, 3])

    async def test_the_source_is_asked_once_per_class(self):
        search, _, _, source = self._search([_hit(1, 0.9), _hit(2, 0.8), _hit(3, 0.7, obj_class="Incident")])

        await search.find(_query(classes=["UserRequest", "Incident"]), _ENGINEER)

        self.assertEqual(
            sorted((obj_class, ids) for _principal, obj_class, ids in source.asked),
            [("Incident", [3]), ("UserRequest", [1, 2])],
        )

    async def test_the_source_is_asked_for_the_principal_it_was_given(self):
        # Rule 9.1: the caller names who is asking and nothing else — there is
        # no callback here to confirm as somebody more privileged (TASK-032)
        search, _, _, source = self._search([_hit(1, 0.9)])

        await search.find(_query(), _ENGINEER)

        self.assertEqual([principal for principal, _cls, _ids in source.asked], [_ENGINEER])

    async def test_a_family_no_source_is_registered_for_is_a_caller_error(self):
        search, _, embedder, _ = self._search([_hit(1, 0.9)])

        with self.assertRaises(UnknownFamily) as raised:
            await search.find(_query(family="kb_articles"), _ENGINEER)

        self.assertIn("kb_articles", str(raised.exception))
        self.assertEqual(raised.exception.known, [_FAMILY])
        # Before an embeddings client is even created: a typo costs no
        # connection and no round trip (TASK-033)
        embedder.embed.assert_not_awaited()
        self.embedder_cls.assert_not_called()

    async def test_a_class_never_confuses_another_classes_ids(self):
        # Same id in two root hierarchies: confirming one must not confirm the other
        search, _, _, source = self._search([_hit(1, 0.9), _hit(1, 0.8, obj_class="KnowledgeBaseArticle")])
        source.confirm_visible = AsyncMock(
            side_effect=lambda principal, obj_class, ids: set(ids) if obj_class == "UserRequest" else set()
        )

        result = await search.find(_query(classes=["UserRequest", "KnowledgeBaseArticle"]), _ENGINEER)

        self.assertEqual([(hit.obj_class, hit.obj_id) for hit in result.hits], [("UserRequest", 1)])

    async def test_top_caps_what_survives_resolution(self):
        search, _, _, _ = self._search([_hit(i, 1.0 - i / 10) for i in range(1, 8)])

        result = await search.find(_query(top=5), _ENGINEER)

        self.assertEqual([hit.obj_id for hit in result.hits], [1, 2, 3, 4, 5])

    async def test_an_empty_index_answers_without_asking_the_source(self):
        search, _, _, source = self._search([])

        result = await search.find(_query(), _ENGINEER)

        self.assertEqual(result.hits, [])
        self.assertEqual(source.asked, [])

    async def test_empty_text_costs_nothing(self):
        search, store, embedder, _ = self._search([_hit(1, 0.9)])

        self.assertEqual((await search.find(_query(text="   "), _ENGINEER)).hits, [])
        embedder.embed.assert_not_awaited()
        store.search.assert_not_awaited()

    async def test_no_classes_searches_the_whole_family(self):
        # classes=None is a valid "whole family" query, not a no-op — the
        # empty-list guard belongs to the query itself (D3, TASK-010)
        search, store, _, _ = self._search([_hit(1, 0.9)])

        await search.find(_query(classes=None), _ENGINEER)

        self.assertIsNone(store.search.await_args.kwargs["classes"])

    async def test_the_family_travels_with_the_query(self):
        # TASK-031: the set is part of the scenario, not bound at construction
        # — one `SimilarSearch` can serve queries against different families.
        # Both are families the default config indexes: a search names a
        # corpus the deployment actually keeps, or it is refused.
        search, store, _, _ = self._search([], families=(_FAMILY, "faq"))

        await search.find(_query(family="faq"), _ENGINEER)

        self.assertEqual(store.search.await_args.kwargs["family"], "faq")


class TestFindStats(_SearchTestCase):
    """TASK-014: the counts the run journal needs, alongside the same hits."""

    async def test_nothing_dropped_by_resolve(self):
        search, _, _, _ = self._search([_hit(1, 0.9), _hit(2, 0.7)])

        result = await search.find(_query(candidates=15), _ENGINEER)

        self.assertEqual([hit.obj_id for hit in result.hits], [1, 2])
        self.assertEqual(result.stats, FindStats(requested=15, found=2, dropped_by_resolve=0))

    async def test_some_dropped_by_resolve(self):
        search, _, _, _ = self._search([_hit(1, 0.9), _hit(2, 0.8), _hit(3, 0.7)], existing={1, 3})

        result = await search.find(_query(candidates=10), _ENGINEER)

        self.assertEqual([hit.obj_id for hit in result.hits], [1, 3])
        self.assertEqual(result.stats, FindStats(requested=10, found=3, dropped_by_resolve=1))

    async def test_empty_store_result(self):
        search, _, _, _ = self._search([])

        result = await search.find(_query(candidates=15), _ENGINEER)

        self.assertEqual(result.hits, [])
        self.assertEqual(result.stats, FindStats(requested=15, found=0, dropped_by_resolve=0))

    async def test_empty_text_still_reports_requested(self):
        search, store, embedder, _ = self._search([_hit(1, 0.9)])

        result = await search.find(_query(text="   ", candidates=15), _ENGINEER)

        self.assertEqual(result.hits, [])
        self.assertEqual(result.stats, FindStats(requested=15, found=0, dropped_by_resolve=0))
        embedder.embed.assert_not_awaited()
        store.search.assert_not_awaited()

    async def test_top_caps_hits_but_stats_count_before_the_cap(self):
        search, _, _, _ = self._search([_hit(i, 1.0 - i / 10) for i in range(1, 8)])

        result = await search.find(_query(candidates=15, top=5), _ENGINEER)

        self.assertEqual(len(result.hits), 5)
        self.assertEqual(result.stats, FindStats(requested=15, found=7, dropped_by_resolve=0))


class TestAvailability(_SearchTestCase):
    """`available()` is the gate a module checks before offering the tool
    that calls `find()` — all four combinations of the three checks."""

    async def test_available_when_everything_is_configured(self):
        search, _, _, _ = self._search([])

        self.assertTrue(await search.available())

    async def test_unavailable_without_a_configured_store(self):
        search, _, _, _ = self._search([], store_configured=False)

        self.assertFalse(await search.available())

    async def test_unavailable_when_indexing_is_disabled(self):
        search, _, _, _ = self._search([], vector_cfg=VectorConfig(enabled=False))

        self.assertFalse(await search.available())

    async def test_unavailable_without_an_embeddings_endpoint(self):
        search, _, _, _ = self._search([], embeddings_cfg=EmbeddingsConfig())

        self.assertFalse(await search.available())

    async def test_unavailable_for_a_switched_off_family(self):
        search, _, _, _ = self._search([], vector_cfg=_family_switched_off())

        self.assertFalse(await search.available(_FAMILY))

    async def test_unavailable_for_a_family_nothing_is_registered_for(self):
        """A config entry outliving its source (renamed in the code, written
        past the UI) must close the gate, not open it onto `UnknownFamily`:
        that exception is not a `ToolRejection`, so it would fail the whole
        run of the consumer that offered the tool."""
        search, _, _, _ = self._search([], families=("faq",))

        self.assertFalse(await search.available(_FAMILY))

    async def test_unavailable_while_the_family_is_being_rebuilt(self):
        """The index answering today was built by another model, so the sweep
        is filling a replacement. Vectors of two models cannot be compared, so
        the corpus is not searchable until it is."""
        search, _, _, _ = self._search([], active_meta=_built_by("old-model"))

        self.assertFalse(await search.available(_FAMILY))

    async def test_available_when_the_index_was_built_by_the_configured_model(self):
        search, _, _, _ = self._search([], active_meta=_built_by("bge-m3"))

        self.assertTrue(await search.available(_FAMILY))

    async def test_the_deployment_gate_ignores_a_rebuild(self):
        """Same split as a switched-off family: "can this deployment search"
        is not the question a rebuild of one corpus answers."""
        search, _, _, _ = self._search([], active_meta=_built_by("old-model"))

        self.assertTrue(await search.available())

    async def test_unavailable_when_the_store_does_not_answer(self):
        """The fingerprint check is the one gate that needs a live store. A
        consumer calls this while assembling a run, before it has done
        anything, so an unreachable Qdrant has to close the gate rather than
        fail the run that would have gone on without the search."""
        search, store, _, _ = self._search([])
        store.active_meta = AsyncMock(side_effect=ConnectionError("qdrant is down"))

        self.assertFalse(await search.available(_FAMILY))

    async def test_the_deployment_gate_ignores_the_family(self):
        """Without a family the question is "can this deployment search at
        all" — telemetry asks it that way, and one corpus switched off is not
        an answer to it."""
        search, _, _, _ = self._search([], vector_cfg=_family_switched_off())

        self.assertTrue(await search.available())


class TestSearchesAreCounted(_SearchTestCase):
    """Whether the vector layer earns its complexity is answered here or
    nowhere: an installation with the layer on and no searches is the answer
    (REQ-009 R3)."""

    async def _counted(self) -> dict:
        return await self.counters.read(datetime.now(UTC).date())

    async def test_a_search_with_hits_counts_as_a_search_and_nothing_else(self):
        search, _, _, _ = self._search([_hit(1, 0.9)])

        await search.find(_query(text="printer is dead"), _ENGINEER)

        counted = await self._counted()
        self.assertEqual(1, counted[Counter.VECTOR_SEARCHES])
        self.assertEqual(0, counted[Counter.VECTOR_SEARCHES_EMPTY])

    async def test_a_search_that_found_nothing_counts_as_both(self):
        search, _, _, _ = self._search([])

        await search.find(_query(text="printer is dead"), _ENGINEER)

        counted = await self._counted()
        self.assertEqual(1, counted[Counter.VECTOR_SEARCHES])
        self.assertEqual(1, counted[Counter.VECTOR_SEARCHES_EMPTY])

    async def test_a_deployment_that_cannot_search_counts_no_search(self):
        """`SearchUnavailable` says the layer is off, not that it looked and
        found nothing — counting it would make "off" look like "useless"."""
        search, _, _, _ = self._search([], store_configured=False)

        with self.assertRaises(SearchUnavailable):
            await search.find(_query(), _ENGINEER)

        self.assertEqual(0, (await self._counted())[Counter.VECTOR_SEARCHES])


class TestSearchUnavailable(_SearchTestCase):
    """`find()` on a deployment that cannot search raises rather than
    answering empty — same three messages the old 409s used to carry."""

    async def test_raises_when_the_store_is_not_configured(self):
        search, _, _, _ = self._search([], store_configured=False)

        with self.assertRaises(SearchUnavailable) as raised:
            await search.find(_query(), _ENGINEER)

        self.assertIn("qdrant_url", str(raised.exception))

    async def test_raises_when_indexing_is_disabled(self):
        search, _, _, _ = self._search([], vector_cfg=VectorConfig(enabled=False))

        with self.assertRaises(SearchUnavailable) as raised:
            await search.find(_query(), _ENGINEER)

        self.assertIn("disabled", str(raised.exception))

    async def test_raises_when_embeddings_are_not_configured(self):
        search, _, _, _ = self._search([], embeddings_cfg=EmbeddingsConfig())

        with self.assertRaises(SearchUnavailable) as raised:
            await search.find(_query(), _ENGINEER)

        self.assertIn("Embeddings", str(raised.exception))

    async def test_raises_for_a_switched_off_family(self):
        """Defence in depth: `available(family)` is the gate a consumer
        checks, this is the answer to one that did not."""
        search, _, _, _ = self._search([_hit(1, 0.9)], vector_cfg=_family_switched_off())

        with self.assertRaises(SearchUnavailable) as raised:
            await search.find(_query(), _ENGINEER)

        self.assertIn(_FAMILY, str(raised.exception))
        self.embedder_cls.assert_not_called()

    async def test_raises_while_the_family_is_being_rebuilt(self):
        """What the backend would answer here is a failure about vector
        widths, somewhere inside a run. This is the same refusal a consumer
        already knows how to take."""
        search, _, _, _ = self._search([_hit(1, 0.9)], active_meta=_built_by("old-model", dim=2560))

        with self.assertRaises(SearchUnavailable) as raised:
            await search.find(_query(), _ENGINEER)

        self.assertIn("rebuilt", str(raised.exception))
        self.assertIn("dim=2560", str(raised.exception))
        self.embedder_cls.assert_not_called()

    async def test_raises_for_a_family_the_config_has_no_entry_for(self):
        """A registered source the config says nothing about is one the sweep
        skips too — the same frozen collection, reached the other way."""
        search, _, _, _ = self._search([_hit(1, 0.9)], vector_cfg=VectorConfig(enabled=True, families={}))

        with self.assertRaises(SearchUnavailable) as raised:
            await search.find(_query(), _ENGINEER)

        self.assertIn(_FAMILY, str(raised.exception))

    async def test_unavailable_before_a_client_is_created(self):
        search, _, _, _ = self._search([], store_configured=False)

        with self.assertRaises(SearchUnavailable):
            await search.find(_query(), _ENGINEER)

        self.embedder_cls.assert_not_called()


class TestEmbeddingsClientLifecycle(_SearchTestCase):
    """Rule 9.4: whoever creates the client closes it — including when the
    store raises partway through `find()`."""

    async def test_the_client_closes_even_when_the_store_raises(self):
        search, store, embedder, _ = self._search([_hit(1, 0.9)])
        store.search.side_effect = RuntimeError("qdrant is down")

        with self.assertRaises(RuntimeError):
            await search.find(_query(), _ENGINEER)

        embedder.aclose.assert_awaited_once()


class TestDateRange(unittest.TestCase):
    """The contract convention (ADR-017) applied to a window: "unrestricted"
    is the absent argument, so an empty or impossible window is a caller
    mistake and says so instead of silently matching everything or nothing."""

    def test_one_bound_is_enough(self):
        self.assertEqual(DateRange(after=_NOW).before, None)
        self.assertEqual(DateRange(before=_NOW).after, None)

    def test_a_window_without_bounds_is_rejected(self):
        with self.assertRaises(ValueError):
            DateRange()

    def test_an_inverted_window_is_rejected(self):
        with self.assertRaises(ValueError):
            DateRange(after=_NOW, before=datetime(2020, 1, 1, tzinfo=UTC))

    def test_a_single_moment_is_a_valid_window(self):
        # Both bounds inclusive — `after == before` means "exactly then", not
        # a guaranteed empty answer
        self.assertEqual(DateRange(after=_NOW, before=_NOW).after, _NOW)
