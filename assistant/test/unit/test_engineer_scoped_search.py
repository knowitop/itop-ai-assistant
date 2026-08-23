"""TASK-015: the two R4 layers composed, without a console to call them yet.

Layer 2 (confirm under the caller's own token) needed no new code — it is
`ItopRepositories.for_principal()` plus `VectorSource.confirm_visible()`, both
already covered by `test_itop_repositories.py` and `test_vector_search.py`. What is new is layer 1
(`AccessRepository.allowed_org_ids()`); this test pins the one thing that is
easy to get backwards when the two are wired together: `None` (unrestricted)
must not become `filters={"org_id": []}` — under ADR-017's convention an empty
list under a present key is a caller error, not "no organizations".

The asymmetry between the layers is deliberate and outlives TASK-032: layer 2
is part of the subsystem's contract (a search cannot run without naming who is
asking), layer 1 stays with the caller — it shapes the walk before it starts,
it is over-permissive by design (ADR-003), and computing it means knowing what
an organization is.
"""

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import fakeredis

from itop_ai_assistant.config import EmbeddingsConfig
from itop_ai_assistant.core.principal import Principal
from itop_ai_assistant.repositories.access import AccessRepository
from itop_ai_assistant.state.counters import DailyCounters
from itop_ai_assistant.vector import VectorConfig
from itop_ai_assistant.vector.ports.query import SearchQuery
from itop_ai_assistant.vector.ports.store import SearchHit
from itop_ai_assistant.vector.use_cases.search import SimilarSearch


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

    async def test_the_prefilter_composes_with_confirmation_under_the_same_principal(self):
        # Layer 1 (org guess) over-includes an org the engineer's own token
        # (layer 2, the source's confirmation) then rejects — exactly the
        # ADR-003 shape: authority stays with the source, the pre-filter only
        # shapes the walk.
        engineer = Principal.delegated("tok", login="ivanov", name="Ivan Ivanov")
        store = MagicMock()
        store.configured = True
        store.search = AsyncMock(
            return_value=[
                SearchHit(obj_class="UserRequest", obj_id=1, score=0.9),
                SearchHit(obj_class="UserRequest", obj_id=2, score=0.8),
            ]
        )
        embedder = MagicMock()
        embedder.embed = AsyncMock(return_value=[[1.0, 0.0]])
        embedder.aclose = AsyncMock()
        # Simulates iTop itself: under the engineer's token, id 2 belongs to an
        # org outside the pre-filter's (stale) guess and is not returned.
        source = MagicMock(name="tickets")
        source.name = "tickets"
        source.confirm_visible = AsyncMock(return_value={1})
        repo = _org_repo([{"allowed_org_id": "3"}])
        config = MagicMock()
        config.get = AsyncMock(
            side_effect=lambda module, model: {
                "vector": VectorConfig(enabled=True),
                "embeddings": EmbeddingsConfig(base_url="http://emb/v1", model="bge-m3"),
            }[module]
        )
        counters = DailyCounters(fakeredis.aioredis.FakeRedis(decode_responses=True))
        search = SimilarSearch(store, config, MagicMock(), counters, sources=[source])

        filters = _org_filter(await repo.allowed_org_ids())
        with patch("itop_ai_assistant.vector.use_cases.search.EmbeddingsClient", return_value=embedder):
            result = await search.find(
                SearchQuery(text="printer", family="tickets", classes=["UserRequest"], filters=filters), engineer
            )

        self.assertEqual(store.search.await_args.kwargs["filters"], {"org_id": ["3"]})
        self.assertEqual([hit.obj_id for hit in result.hits], [1])
        self.assertEqual(result.stats.dropped_by_resolve, 1)
        # Both layers under the same identity — the thing that used to depend
        # on the caller passing the right callback
        self.assertEqual(source.confirm_visible.await_args.args[0], engineer)


if __name__ == "__main__":
    unittest.main()
