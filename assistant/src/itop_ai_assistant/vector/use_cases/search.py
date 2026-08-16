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

The caller passes a `SearchQuery` and the principal to answer for — nothing
else. The confirmation itself is not theirs to supply: it used to arrive as a
`resolve` callback, which meant a caller could pass a function that confirms
as somebody else (or as the service account) and the types would not notice
(rule 9.1, TASK-032). What a caller names now is *who is asking*; how that
becomes a confirmation is this module's business.

Source-agnostic like the rest of `vector/`: nothing here knows what a ticket
is. `Principal` is the one name it borrows from the platform — see
ADR-021 for why that is not the same as knowing a consumer's domain.
"""

import logging
from collections.abc import Sequence
from typing import Protocol

from itop_ai_assistant.config import VectorConfig
from itop_ai_assistant.core.principal import Principal
from itop_ai_assistant.vector.adapters.embedder import EmbeddingsClient
from itop_ai_assistant.vector.ports.query import FindStats, ObjectHit, SearchQuery, SearchResult
from itop_ai_assistant.vector.ports.store import ChunkStore, SearchHit
from itop_ai_assistant.vector.sources.registry import ItopRepos, build_vector_sources

logger = logging.getLogger(__name__)


class CandidateSource(Protocol):
    """What the read path needs of a source: which family it answers for, and
    whether a given person may see these ids.

    Two members out of `VectorSource`'s ten, declared here rather than
    imported whole — the same cut `IndexerDeps` and `ItopRepos` make (rule
    3.3, port sliced by consumer). Sweeping, chunking and the source's
    declared vocabulary are the indexer's business; a search that could name
    them could also call them.
    """

    @property
    def name(self) -> str: ...

    async def confirm_visible(self, principal: Principal, obj_class: str, ids: list[int]) -> set[int]: ...


class UnknownFamily(ValueError):
    """The query names a family no source is registered for.

    A caller error, not a search result: `known` is carried so a transport can
    say what it should have asked for.
    """

    def __init__(self, family: str, known: Sequence[str]) -> None:
        self.family = family
        self.known = sorted(known)
        super().__init__(f"Unknown family {family!r}; known: {self.known}")


class SimilarSearch:
    """One search over the vector index, confirmed against its source.

    The scenario — including which family to search — travels in the
    `SearchQuery` rather than being fixed at construction (TASK-031): it is
    the caller's configuration, not a property of this object. Picking the
    source that family names is this object's own job, which is why it takes
    the registry's ingredients rather than a ready source: a caller that had
    to pick one would need `vector/sources/` in its imports, and the next
    caller would pick differently (rule 6.5 — the family name comes from the
    consumer's own configuration, and the subsystem validates it).

    `sources` is the injection point for tests, exactly as in
    `use_cases/indexer.py`: production passes nothing and gets what the
    registry builds from `itop`/`cfg`.
    """

    def __init__(
        self,
        store: ChunkStore,
        embedder: EmbeddingsClient,
        itop: ItopRepos,
        cfg: VectorConfig,
        *,
        sources: Sequence[CandidateSource] | None = None,
    ) -> None:
        self._store = store
        self._embedder = embedder
        built = sources if sources is not None else build_vector_sources(itop, cfg)
        self._sources: dict[str, CandidateSource] = {source.name: source for source in built}

    async def find(self, query: SearchQuery, principal: Principal) -> SearchResult:
        """Objects most similar to `query.text` that `principal` may see.

        Best first, at most `top`. Returns no hits for empty text and for an
        index that has nothing to say — a caller with no results is a normal
        outcome here, not a failure. `stats` carries what the call saw, for
        the run journal (TASK-014); every scenario parameter has already been
        validated by `SearchQuery` itself.

        Raises `UnknownFamily` when nothing is registered for `query.family` —
        before embedding anything, so a typo costs no round trip.
        """
        source = self._sources.get(query.family)
        if source is None:
            raise UnknownFamily(query.family, list(self._sources))
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
        kept = await self._keep_confirmed(hits, source, principal)
        stats = FindStats(requested=query.candidates, found=len(hits), dropped_by_resolve=len(hits) - len(kept))
        return SearchResult(hits=kept[: query.top], stats=stats)

    async def _keep_confirmed(
        self, hits: list[SearchHit], source: CandidateSource, principal: Principal
    ) -> list[ObjectHit]:
        """Drop candidates the source does not confirm for `principal`, keeping the order.

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
            (obj_class, obj_id)
            for obj_class, ids in by_class.items()
            for obj_id in await source.confirm_visible(principal, obj_class, ids)
        }

        kept = [
            ObjectHit(obj_class=hit.obj_class, obj_id=hit.obj_id, score=hit.score)
            for hit in hits
            if (hit.obj_class, hit.obj_id) in existing
        ]
        if len(kept) < len(hits):
            logger.info(f"similar search: {len(hits) - len(kept)} of {len(hits)} candidates dropped by the source")
        return kept
