"""Similar-object search: the read path over the index the sweep builds.

Three steps, in this order (ADR-003 in dev-docs): embed the query text,
walk the index with the filters applied *during* the walk, then hand the
candidate ids back to the source and keep only what it still returns. The
last step is what makes the answer safe: the index is built by the service
account and is global, so a hit is a **candidate** until the source, asked
under the caller's own principal, confirms it.

That is also why more candidates are asked for than are returned — the
`candidates`/`top` pair. Filtering after the fact is exactly what R1 warns
against, but this particular filter cannot be pushed into the walk: it is
computed by iTop, not by us.

Source-agnostic like the rest of `vector/`: the caller passes a
`SearchQuery` and a `resolve` callback. Nothing here knows what a ticket is.
"""

import logging
from collections.abc import Awaitable, Callable

from itop_ai_assistant.vector.adapters.embedder import EmbeddingsClient
from itop_ai_assistant.vector.ports.query import FindStats, ObjectHit, SearchQuery, SearchResult
from itop_ai_assistant.vector.ports.store import ChunkStore, SearchHit

logger = logging.getLogger(__name__)

# Which ids of this class still exist and are visible to whoever is asking
Resolver = Callable[[str, list[int]], Awaitable[set[int]]]


class SimilarSearch:
    """One search over the vector index, resolved against its source.

    The scenario — including which family to search — travels in the
    `SearchQuery` rather than being fixed at construction (TASK-031): it is
    the caller's configuration, not a property of this object. What the query
    cannot do is name a family that has no registered source; nothing checks
    that here, because a registry built for the check would duplicate
    `vector/sources/registry.py` (the very duplication D1/D2 avoid) — today
    the caller that picks the source does the checking.
    """

    def __init__(self, store: ChunkStore, embedder: EmbeddingsClient, resolve: Resolver) -> None:
        self._store = store
        self._embedder = embedder
        self._resolve = resolve

    async def find(self, query: SearchQuery) -> SearchResult:
        """Objects most similar to `query.text`, best first, at most `top`.

        Returns no hits for empty text and for an index that has nothing to
        say — a caller with no results is a normal outcome here, not a
        failure. `stats` carries what the call saw, for the run journal
        (TASK-014); every scenario parameter has already been validated by
        `SearchQuery` itself.
        """
        empty = SearchResult(hits=[], stats=FindStats(requested=query.candidates, found=0, dropped_by_resolve=0))
        if not query.text.strip():
            return empty
        embedding = (await self._embedder.embed([query.text]))[0]
        hits = await self._store.search(
            embedding,
            family=query.family,
            classes=query.classes,
            chunk_kinds=query.chunk_kinds,
            filters=query.filters,
            visibilities=list(query.visibilities),
            exclude=query.exclude,
            created=query.created,
            updated=query.updated,
            score_threshold=query.min_score,
            limit=query.candidates,
        )
        if not hits:
            return empty
        kept = await self._keep_resolvable(hits)
        stats = FindStats(requested=query.candidates, found=len(hits), dropped_by_resolve=len(hits) - len(kept))
        return SearchResult(hits=kept[: query.top], stats=stats)

    async def _keep_resolvable(self, hits: list[SearchHit]) -> list[ObjectHit]:
        """Drop candidates the source no longer returns, keeping the order.

        One call per class rather than one per object: the probe takes a
        list of ids. The share dropped here is the metric ADR-003 asks for —
        it says how far the pre-filter has drifted from the real rights.
        Capping to `top` happens in `find()`, after this — here the count
        must stay uncontaminated by that cut, or `FindStats.dropped_by_resolve`
        (TASK-014) would report "dropped by the source" for objects the top-N
        limit dropped.
        """
        by_class: dict[str, list[int]] = {}
        for hit in hits:
            by_class.setdefault(hit.obj_class, []).append(hit.obj_id)
        existing = {
            (obj_class, obj_id) for obj_class, ids in by_class.items() for obj_id in await self._resolve(obj_class, ids)
        }

        kept = [
            ObjectHit(obj_class=hit.obj_class, obj_id=hit.obj_id, score=hit.score)
            for hit in hits
            if (hit.obj_class, hit.obj_id) in existing
        ]
        if len(kept) < len(hits):
            logger.info(f"similar search: {len(hits) - len(kept)} of {len(hits)} candidates dropped by the source")
        return kept
