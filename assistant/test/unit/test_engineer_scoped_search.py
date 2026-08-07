"""TASK-015: the two R4 layers composed, without a console to call them yet.

Layer 2 (resolve under the caller's own token) needed no new code — it is
`ItopProvider.for_principal()` plus `SimilarSearch(resolve=...)`, both already
covered by `test_deps.py` and `test_vector_search.py`. What is new is layer 1
(`AccessRepository.allowed_org_ids()`); this test pins the one thing that is
easy to get backwards when the two are wired together: `None` (unrestricted)
must not become `filters={"org_id": []}` — under ADR-017's convention an empty
list under a present key is a caller error, not "no organizations".
"""

import unittest
from unittest.mock import AsyncMock, MagicMock

from itop_ai_assistant.access_repository import AccessRepository
from itop_ai_assistant.vector.search import SimilarSearch
from itop_ai_assistant.vector.store import SearchHit


def _org_repo(allowed: list[dict] | None) -> AccessRepository:
    schema = MagicMock()
    schema.find_one = AsyncMock(return_value=None if allowed is None else {"allowed_org_list": allowed})
    itop = MagicMock()
    itop.schema = MagicMock(return_value=schema)
    return AccessRepository(itop)


def _org_filter(org_ids: list[str] | None) -> dict[str, list[str]] | None:
    """What a caller (the future console) does with the repository's answer."""
    return {"org_id": org_ids} if org_ids is not None else None


class TestOrgPrefilterFeedsSearch(unittest.IsolatedAsyncioTestCase):
    async def test_an_unrestricted_principal_adds_no_org_filter(self):
        repo = _org_repo([])

        filters = _org_filter(await repo.allowed_org_ids())

        self.assertIsNone(filters)

    async def test_a_scoped_principal_narrows_by_org(self):
        repo = _org_repo([{"allowed_org_id": "3"}, {"allowed_org_id": "9"}])

        filters = _org_filter(await repo.allowed_org_ids())

        self.assertEqual(filters, {"org_id": ["3", "9"]})

    async def test_the_prefilter_composes_with_resolve_under_the_same_principal(self):
        # Layer 1 (org guess) over-includes an org the engineer's own token
        # (layer 2, `resolve`) then rejects — exactly the ADR-003 shape:
        # authority stays with the source, the pre-filter only shapes the walk.
        store = MagicMock()
        store.search = AsyncMock(
            return_value=[
                SearchHit(obj_class="UserRequest", obj_id=1, score=0.9),
                SearchHit(obj_class="UserRequest", obj_id=2, score=0.8),
            ]
        )
        embedder = MagicMock()
        embedder.embed = AsyncMock(return_value=[[1.0, 0.0]])
        # Simulates iTop itself: under the engineer's token, id 2 belongs to an
        # org outside the pre-filter's (stale) guess and is not returned.
        resolve = AsyncMock(return_value={1})
        repo = _org_repo([{"allowed_org_id": "3"}])
        search = SimilarSearch(store, embedder, resolve, family="tickets")

        filters = _org_filter(await repo.allowed_org_ids())
        hits, stats = await search.find_with_stats("printer", classes=["UserRequest"], filters=filters)

        self.assertEqual(store.search.await_args.kwargs["filters"], {"org_id": ["3"]})
        self.assertEqual([hit.obj_id for hit in hits], [1])
        self.assertEqual(stats.dropped_by_resolve, 1)


if __name__ == "__main__":
    unittest.main()
