"""The read path: candidates from the index, confirmed by the source."""

import unittest
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from itop_ai_assistant.vector.ports.query import FindStats, ObjectHit, SearchQuery
from itop_ai_assistant.vector.ports.store import DateRange, SearchHit
from itop_ai_assistant.vector.use_cases.search import SimilarSearch

_NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
_FAMILY = "tickets"


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


def _search(hits: list[SearchHit], *, existing: set[int] | None = None) -> tuple[SimilarSearch, MagicMock, MagicMock]:
    store = MagicMock()
    store.search = AsyncMock(return_value=hits)
    embedder = MagicMock()
    embedder.embed = AsyncMock(return_value=[[1.0, 0.0]])
    resolve = AsyncMock(side_effect=lambda obj_class, ids: set(ids) if existing is None else existing & set(ids))
    return SimilarSearch(store, embedder, resolve), store, embedder


class TestSimilarSearch(unittest.IsolatedAsyncioTestCase):
    async def test_returns_hits_best_first(self):
        search, _, _ = _search([_hit(1, 0.9), _hit(2, 0.7)])

        result = await search.find(_query(text="printer is dead"))

        self.assertEqual(result.hits, [ObjectHit("UserRequest", 1, 0.9), ObjectHit("UserRequest", 2, 0.7)])

    async def test_the_query_is_embedded_once(self):
        search, _, embedder = _search([_hit(1, 0.9)])

        await search.find(_query(text="printer is dead"))

        embedder.embed.assert_awaited_once_with(["printer is dead"])

    async def test_filters_reach_the_store(self):
        search, store, _ = _search([])

        await search.find(
            _query(
                classes=["UserRequest", "Incident"],
                filters={"status": ["resolved", "closed"]},
                exclude=("UserRequest", 7),
                updated=DateRange(after=_NOW),
                candidates=15,
            )
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
        search, store, _ = _search([])
        created = DateRange(after=datetime(2020, 1, 1, tzinfo=UTC), before=_NOW)

        await search.find(_query(created=created, updated=DateRange(after=_NOW)))

        kwargs = store.search.await_args.kwargs
        self.assertEqual(kwargs["created"], created)
        self.assertEqual(kwargs["updated"], DateRange(after=_NOW))

    async def test_date_windows_default_to_none(self):
        search, store, _ = _search([_hit(1, 0.9)])

        await search.find(_query())

        self.assertIsNone(store.search.await_args.kwargs["created"])
        self.assertIsNone(store.search.await_args.kwargs["updated"])

    async def test_chunk_kinds_reaches_the_store_under_the_same_name(self):
        search, store, _ = _search([])

        await search.find(_query(chunk_kinds=["profile", "body"]))

        self.assertEqual(store.search.await_args.kwargs["chunk_kinds"], ["profile", "body"])

    async def test_chunk_kinds_defaults_to_none(self):
        search, store, _ = _search([_hit(1, 0.9)])

        await search.find(_query())

        self.assertIsNone(store.search.await_args.kwargs["chunk_kinds"])

    async def test_min_score_reaches_the_store_as_score_threshold(self):
        search, store, _ = _search([])

        await search.find(_query(min_score=0.5))

        self.assertEqual(store.search.await_args.kwargs["score_threshold"], 0.5)

    async def test_min_score_defaults_to_no_floor(self):
        search, store, _ = _search([_hit(1, 0.9)])

        await search.find(_query())

        self.assertIsNone(store.search.await_args.kwargs["score_threshold"])

    async def test_candidates_the_source_no_longer_returns_are_dropped(self):
        # The pre-filter is deliberately over-permissive (ADR-003); the source
        # is the authority on what may be quoted
        search, _, _ = _search([_hit(1, 0.9), _hit(2, 0.8), _hit(3, 0.7)], existing={1, 3})

        result = await search.find(_query())

        self.assertEqual([hit.obj_id for hit in result.hits], [1, 3])

    async def test_the_source_is_asked_once_per_class(self):
        search, _, _ = _search([_hit(1, 0.9), _hit(2, 0.8), _hit(3, 0.7, obj_class="Incident")])
        resolve = search._resolve

        await search.find(_query(classes=["UserRequest", "Incident"]))

        self.assertEqual(
            sorted((call.args[0], sorted(call.args[1])) for call in resolve.await_args_list),
            [("Incident", [3]), ("UserRequest", [1, 2])],
        )

    async def test_a_class_never_confuses_another_classes_ids(self):
        # Same id in two root hierarchies: confirming one must not confirm the other
        search, _, _ = _search([_hit(1, 0.9), _hit(1, 0.8, obj_class="KnowledgeBaseArticle")])
        search._resolve = AsyncMock(
            side_effect=lambda obj_class, ids: set(ids) if obj_class == "UserRequest" else set()
        )

        result = await search.find(_query(classes=["UserRequest", "KnowledgeBaseArticle"]))

        self.assertEqual([(hit.obj_class, hit.obj_id) for hit in result.hits], [("UserRequest", 1)])

    async def test_top_caps_what_survives_resolution(self):
        search, _, _ = _search([_hit(i, 1.0 - i / 10) for i in range(1, 8)])

        result = await search.find(_query(top=5))

        self.assertEqual([hit.obj_id for hit in result.hits], [1, 2, 3, 4, 5])

    async def test_an_empty_index_answers_without_asking_the_source(self):
        search, _, _ = _search([])

        result = await search.find(_query())

        self.assertEqual(result.hits, [])
        search._resolve.assert_not_awaited()

    async def test_empty_text_costs_nothing(self):
        search, store, embedder = _search([_hit(1, 0.9)])

        self.assertEqual((await search.find(_query(text="   "))).hits, [])
        embedder.embed.assert_not_awaited()
        store.search.assert_not_awaited()

    async def test_no_classes_searches_the_whole_family(self):
        # classes=None is a valid "whole family" query, not a no-op — the
        # empty-list guard belongs to the query itself (D3, TASK-010)
        search, store, _ = _search([_hit(1, 0.9)])

        await search.find(_query(classes=None))

        self.assertIsNone(store.search.await_args.kwargs["classes"])

    async def test_the_family_travels_with_the_query(self):
        # TASK-031: the set is part of the scenario, not bound at construction
        # — one `SimilarSearch` can serve queries against different families
        search, store, _ = _search([])

        await search.find(_query(family="kb_articles"))

        self.assertEqual(store.search.await_args.kwargs["family"], "kb_articles")


class TestFindStats(unittest.IsolatedAsyncioTestCase):
    """TASK-014: the counts the run journal needs, alongside the same hits."""

    async def test_nothing_dropped_by_resolve(self):
        search, _, _ = _search([_hit(1, 0.9), _hit(2, 0.7)])

        result = await search.find(_query(candidates=15))

        self.assertEqual([hit.obj_id for hit in result.hits], [1, 2])
        self.assertEqual(result.stats, FindStats(requested=15, found=2, dropped_by_resolve=0))

    async def test_some_dropped_by_resolve(self):
        search, _, _ = _search([_hit(1, 0.9), _hit(2, 0.8), _hit(3, 0.7)], existing={1, 3})

        result = await search.find(_query(candidates=10))

        self.assertEqual([hit.obj_id for hit in result.hits], [1, 3])
        self.assertEqual(result.stats, FindStats(requested=10, found=3, dropped_by_resolve=1))

    async def test_empty_store_result(self):
        search, _, _ = _search([])

        result = await search.find(_query(candidates=15))

        self.assertEqual(result.hits, [])
        self.assertEqual(result.stats, FindStats(requested=15, found=0, dropped_by_resolve=0))

    async def test_empty_text_still_reports_requested(self):
        search, store, embedder = _search([_hit(1, 0.9)])

        result = await search.find(_query(text="   ", candidates=15))

        self.assertEqual(result.hits, [])
        self.assertEqual(result.stats, FindStats(requested=15, found=0, dropped_by_resolve=0))
        embedder.embed.assert_not_awaited()
        store.search.assert_not_awaited()

    async def test_top_caps_hits_but_stats_count_before_the_cap(self):
        search, _, _ = _search([_hit(i, 1.0 - i / 10) for i in range(1, 8)])

        result = await search.find(_query(candidates=15, top=5))

        self.assertEqual(len(result.hits), 5)
        self.assertEqual(result.stats, FindStats(requested=15, found=7, dropped_by_resolve=0))


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
