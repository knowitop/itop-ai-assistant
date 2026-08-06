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

Source-agnostic like the rest of `vector/`: the caller passes the query
text, the filter values and a `resolve` callback. Nothing here knows what a
ticket is.
"""

import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime

from itop_ai_assistant.vector.embedder import EmbeddingsClient
from itop_ai_assistant.vector.store import ChunkStore, SearchHit

logger = logging.getLogger(__name__)

# Which ids of this class still exist and are visible to whoever is asking
Resolver = Callable[[str, list[int]], Awaitable[set[int]]]


@dataclass(frozen=True)
class ObjectHit:
    obj_class: str
    obj_id: int
    score: float


class SimilarSearch:
    """One search over the vector index, resolved against its source.

    `family` is fixed at construction, not passed to `find()`: the caller
    that builds a `SimilarSearch` already commits to one business scenario
    (the same place `resolve` gets bound, e.g. `ticket_repo.find_existing_ids`
    in `pipeline.py`) — a wrong `family` becomes a mistake at that one
    construction site instead of a risk on every call (D5, TASK-008). No
    registry checks that `classes` actually belongs to `family`: building one
    here would duplicate `vector_sources/registry.py`, the very duplication
    D1/D2 avoid.
    """

    def __init__(self, store: ChunkStore, embedder: EmbeddingsClient, resolve: Resolver, *, family: str) -> None:
        self._store = store
        self._embedder = embedder
        self._resolve = resolve
        self._family = family

    async def find(
        self,
        text: str,
        *,
        classes: list[str] | None = None,
        filters: dict[str, list[str]] | None = None,
        visibilities: Sequence[str] = ("public", "internal"),
        exclude: tuple[str, int] | None = None,
        updated_after: datetime | None = None,
        candidates: int = 15,
        top: int = 5,
    ) -> list[ObjectHit]:
        """Objects most similar to `text`, best first, at most `top` of them.

        Returns [] for empty text and for an index that has nothing to say —
        a caller with no results is a normal outcome here, not a failure.
        """
        if not text.strip():
            return []
        embedding = (await self._embedder.embed([text]))[0]
        hits = await self._store.search(
            embedding,
            family=self._family,
            classes=classes,
            filters=filters,
            visibilities=list(visibilities),
            exclude=exclude,
            updated_after=updated_after,
            limit=candidates,
        )
        if not hits:
            return []
        return await self._keep_resolvable(hits, top)

    async def _keep_resolvable(self, hits: list[SearchHit], top: int) -> list[ObjectHit]:
        """Drop candidates the source no longer returns, keeping the order.

        One call per class rather than one per object: the probe takes a
        list of ids. The share dropped here is the metric ADR-003 asks for —
        it says how far the pre-filter has drifted from the real rights.
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
        return kept[:top]
