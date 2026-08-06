"""The read path: candidates from the index, confirmed by the source."""

import unittest
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

from itop_ai_assistant.vector.search import ObjectHit, SimilarSearch
from itop_ai_assistant.vector.store import SearchHit

_NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
_FAMILY = "tickets"


def _hit(obj_id: int, score: float, obj_class: str = "UserRequest") -> SearchHit:
    return SearchHit(obj_class=obj_class, obj_id=obj_id, score=score)


def _search(
    hits: list[SearchHit], *, existing: set[int] | None = None, family: str = _FAMILY
) -> tuple[SimilarSearch, MagicMock, MagicMock]:
    store = MagicMock()
    store.search = AsyncMock(return_value=hits)
    embedder = MagicMock()
    embedder.embed = AsyncMock(return_value=[[1.0, 0.0]])
    resolve = AsyncMock(side_effect=lambda obj_class, ids: set(ids) if existing is None else existing & set(ids))
    return SimilarSearch(store, embedder, resolve, family=family), store, embedder


class TestSimilarSearch(unittest.IsolatedAsyncioTestCase):
    async def test_returns_hits_best_first(self):
        search, _, _ = _search([_hit(1, 0.9), _hit(2, 0.7)])

        found = await search.find("printer is dead", classes=["UserRequest"], filters={"status": ["resolved"]})

        self.assertEqual(found, [ObjectHit("UserRequest", 1, 0.9), ObjectHit("UserRequest", 2, 0.7)])

    async def test_the_query_is_embedded_once(self):
        search, _, embedder = _search([_hit(1, 0.9)])

        await search.find("printer is dead", classes=["UserRequest"], filters={"status": ["resolved"]})

        embedder.embed.assert_awaited_once_with(["printer is dead"])

    async def test_filters_reach_the_store(self):
        search, store, _ = _search([])

        await search.find(
            "q",
            classes=["UserRequest", "Incident"],
            filters={"status": ["resolved", "closed"]},
            exclude=("UserRequest", 7),
            updated_after=_NOW,
            candidates=15,
        )

        kwargs = store.search.await_args.kwargs
        self.assertEqual(kwargs["family"], _FAMILY)
        self.assertEqual(kwargs["classes"], ["UserRequest", "Incident"])
        self.assertEqual(kwargs["filters"], {"status": ["resolved", "closed"]})
        self.assertEqual(kwargs["visibilities"], ["public", "internal"])
        self.assertEqual(kwargs["exclude"], ("UserRequest", 7))
        self.assertEqual(kwargs["updated_after"], _NOW)
        self.assertEqual(kwargs["limit"], 15)

    async def test_candidates_the_source_no_longer_returns_are_dropped(self):
        # The pre-filter is deliberately over-permissive (ADR-003); the source
        # is the authority on what may be quoted
        search, _, _ = _search([_hit(1, 0.9), _hit(2, 0.8), _hit(3, 0.7)], existing={1, 3})

        found = await search.find("q", classes=["UserRequest"], filters={"status": ["resolved"]})

        self.assertEqual([hit.obj_id for hit in found], [1, 3])

    async def test_the_source_is_asked_once_per_class(self):
        search, _, _ = _search([_hit(1, 0.9), _hit(2, 0.8), _hit(3, 0.7, obj_class="Incident")])
        resolve = search._resolve

        await search.find("q", classes=["UserRequest", "Incident"], filters={"status": ["resolved"]})

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

        found = await search.find(
            "q", classes=["UserRequest", "KnowledgeBaseArticle"], filters={"status": ["resolved"]}
        )

        self.assertEqual([(hit.obj_class, hit.obj_id) for hit in found], [("UserRequest", 1)])

    async def test_top_caps_what_survives_resolution(self):
        search, _, _ = _search([_hit(i, 1.0 - i / 10) for i in range(1, 8)])

        found = await search.find("q", classes=["UserRequest"], filters={"status": ["resolved"]}, top=5)

        self.assertEqual([hit.obj_id for hit in found], [1, 2, 3, 4, 5])

    async def test_an_empty_index_answers_without_asking_the_source(self):
        search, _, _ = _search([])

        found = await search.find("q", classes=["UserRequest"], filters={"status": ["resolved"]})

        self.assertEqual(found, [])
        search._resolve.assert_not_awaited()

    async def test_empty_text_costs_nothing(self):
        search, store, embedder = _search([_hit(1, 0.9)])

        self.assertEqual(await search.find("   ", classes=["UserRequest"], filters={"status": ["resolved"]}), [])
        embedder.embed.assert_not_awaited()
        store.search.assert_not_awaited()

    async def test_no_classes_searches_the_whole_family(self):
        # classes=None is a valid "whole family" query, not a no-op — the
        # empty-list guard lives in the store, not here (D3, TASK-010)
        search, store, _ = _search([_hit(1, 0.9)])

        await search.find("q", filters={"status": ["resolved"]})

        self.assertIsNone(store.search.await_args.kwargs["classes"])

    async def test_the_constructed_family_reaches_every_call_regardless_of_find_args(self):
        # `family` is bound once at construction (D5, TASK-008) — no `find()`
        # argument can steer a search to a different collection
        search, store, _ = _search([], family="kb_articles")

        await search.find("q", classes=["UserRequest"], filters={"status": ["resolved"]})

        self.assertEqual(store.search.await_args.kwargs["family"], "kb_articles")
