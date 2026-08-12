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

from itop_ai_assistant.vector.adapters.embedder import EmbeddingsClient
from itop_ai_assistant.vector.ports.store import ChunkStore, DateRange, SearchHit

logger = logging.getLogger(__name__)

# Which ids of this class still exist and are visible to whoever is asking
Resolver = Callable[[str, list[int]], Awaitable[set[int]]]


@dataclass(frozen=True)
class ObjectHit:
    obj_class: str
    obj_id: int
    score: float


@dataclass(frozen=True)
class FindStats:
    """What a `find()` call actually saw, for the run journal (TASK-014).

    `found < requested` is a signal, not proof: it happens both when
    `min_score` cut candidates and when the index/filter combination has
    fewer matching objects than `requested` to begin with — Qdrant's
    `query_points_groups` applies `score_threshold` natively and returns no
    count of what it dropped, so telling the two apart exactly would need a
    second query. `dropped_by_resolve` is the ADR-003 metric.
    """

    requested: int
    found: int
    dropped_by_resolve: int


class SimilarSearch:
    """One search over the vector index, resolved against its source.

    `family` is fixed at construction, not passed to `find()`: the caller
    that builds a `SimilarSearch` already commits to one business scenario
    (the same place `resolve` gets bound, e.g. `ticket_repo.find_existing_ids`
    in `compose.py`) — a wrong `family` becomes a mistake at that one
    construction site instead of a risk on every call (D5, TASK-008). No
    registry checks that `classes` actually belongs to `family`: building one
    here would duplicate `vector/sources/registry.py`, the very duplication
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
        chunk_kinds: list[str] | None = None,
        filters: dict[str, list[str]] | None = None,
        visibilities: Sequence[str] = ("public", "internal"),
        exclude: tuple[str, int] | None = None,
        created: DateRange | None = None,
        updated: DateRange | None = None,
        min_score: float | None = None,
        candidates: int = 15,
        top: int = 5,
    ) -> list[ObjectHit]:
        """Objects most similar to `text`, best first, at most `top` of them.

        Returns [] for empty text and for an index that has nothing to say —
        a caller with no results is a normal outcome here, not a failure.
        `created`/`updated` are inclusive windows over the two system dates,
        either bound optional (`DateRange`). `min_score` drops a candidate
        below that similarity score regardless
        of rank — top-N alone does not mean relevant (TASK-011); `None`
        applies no floor. Named `min_score` rather than the backend's own
        `score_threshold` (`ChunkStore.search()`) to keep this port
        backend-agnostic, same as `candidates` translating to `limit`.
        """
        hits, _ = await self._find(
            text,
            classes=classes,
            chunk_kinds=chunk_kinds,
            filters=filters,
            visibilities=visibilities,
            exclude=exclude,
            created=created,
            updated=updated,
            min_score=min_score,
            candidates=candidates,
            top=top,
        )
        return hits

    async def find_with_stats(
        self,
        text: str,
        *,
        classes: list[str] | None = None,
        chunk_kinds: list[str] | None = None,
        filters: dict[str, list[str]] | None = None,
        visibilities: Sequence[str] = ("public", "internal"),
        exclude: tuple[str, int] | None = None,
        created: DateRange | None = None,
        updated: DateRange | None = None,
        min_score: float | None = None,
        candidates: int = 15,
        top: int = 5,
    ) -> tuple[list[ObjectHit], FindStats]:
        """Same as `find()`, plus the counts a caller needs to journal (TASK-014)."""
        return await self._find(
            text,
            classes=classes,
            chunk_kinds=chunk_kinds,
            filters=filters,
            visibilities=visibilities,
            exclude=exclude,
            created=created,
            updated=updated,
            min_score=min_score,
            candidates=candidates,
            top=top,
        )

    async def _find(
        self,
        text: str,
        *,
        classes: list[str] | None,
        chunk_kinds: list[str] | None,
        filters: dict[str, list[str]] | None,
        visibilities: Sequence[str],
        exclude: tuple[str, int] | None,
        created: DateRange | None,
        updated: DateRange | None,
        min_score: float | None,
        candidates: int,
        top: int,
    ) -> tuple[list[ObjectHit], FindStats]:
        if not text.strip():
            return [], FindStats(requested=candidates, found=0, dropped_by_resolve=0)
        embedding = (await self._embedder.embed([text]))[0]
        hits = await self._store.search(
            embedding,
            family=self._family,
            classes=classes,
            chunk_kinds=chunk_kinds,
            filters=filters,
            visibilities=list(visibilities),
            exclude=exclude,
            created=created,
            updated=updated,
            score_threshold=min_score,
            limit=candidates,
        )
        if not hits:
            return [], FindStats(requested=candidates, found=0, dropped_by_resolve=0)
        kept = await self._keep_resolvable(hits)
        stats = FindStats(requested=candidates, found=len(hits), dropped_by_resolve=len(hits) - len(kept))
        return kept[:top], stats

    async def _keep_resolvable(self, hits: list[SearchHit]) -> list[ObjectHit]:
        """Drop candidates the source no longer returns, keeping the order.

        One call per class rather than one per object: the probe takes a
        list of ids. The share dropped here is the metric ADR-003 asks for —
        it says how far the pre-filter has drifted from the real rights.
        Capping to `top` is the caller's job — this only tells resolved apart
        from dropped, which `FindStats.dropped_by_resolve` (TASK-014) needs
        uncontaminated by the separate, later top-N cut.
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
