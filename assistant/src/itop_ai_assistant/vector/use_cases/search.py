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

This is the subsystem's door (TASK-033): the details a caller used to bring —
an embeddings client and its lifetime, the availability gates, the source
picked from `query.family` — no longer arrive from outside. The door reads its
own configuration on every call (an admin edit to the family list or the
embeddings endpoint must not need a restart) and owns the embeddings client
for the length of one `find()` (rule 9.4 — whoever creates a resource closes
it).

Source-agnostic like the rest of `vector/`: nothing here knows what a ticket
is. `Principal` is the one name it borrows from the platform — see
ADR-021 for why that is not the same as knowing a consumer's domain.
"""

import logging
from collections.abc import Callable, Sequence
from typing import Protocol

from itop_ai_assistant.config import EmbeddingsConfig
from itop_ai_assistant.core.principal import Principal
from itop_ai_assistant.settings.config_store import ConfigStore
from itop_ai_assistant.vector.adapters.embedder import EmbeddingsClient
from itop_ai_assistant.vector.config import VectorConfig
from itop_ai_assistant.vector.ports.query import FindStats, ObjectHit, SearchQuery, SearchResult
from itop_ai_assistant.vector.ports.store import ChunkStore, SearchHit

logger = logging.getLogger(__name__)


class CandidateSource(Protocol):
    """What the read path needs of a source: which family it answers for, and
    whether a given person may see these ids.

    Two members out of `VectorSource`'s ten, declared here rather than
    imported whole — the same cut `ItopRepos` makes (rule 3.3, port sliced by
    consumer). Sweeping, chunking and the source's declared vocabulary are the
    indexer's business; a search that could name them could also call them.

    Stays a `Protocol` where the indexer's five-member, disjoint slice does
    not (TASK-039): two closely related members describing one source versus
    five unrelated dependencies of a sweep — rule 3.2's actual criterion is
    cohesion, not member count.
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


class SearchUnavailable(RuntimeError):
    """This deployment cannot search right now — a caller/deployment error, not
    a search result. The three messages match the ones `/search` used to raise
    as 409s by hand."""


class SimilarSearch:
    """One search over the vector index, confirmed against its source.

    The scenario — including which family to search — travels in the
    `SearchQuery` rather than being fixed at construction: it is the caller's
    configuration, not a property of this object. Picking the source that
    family names is this object's own job, which is why it takes a builder
    rather than a ready source: a caller that had to pick one would need
    `content_sources/` in its imports, and the next caller would pick
    differently (rule 6.5 — the family name comes from the consumer's own
    configuration, and the subsystem validates it).

    Long-lived (one instance for the process, via `AppDeps.vector_search`),
    unlike what it reads and owns per call: `config` is re-read on every
    `find()`/`available()` so an admin edit to the family list or the
    embeddings endpoint applies without a restart, and the embeddings client
    is created and closed around one `find()` — the door creates it, the door
    closes it (rule 9.4).

    `build_sources` is a `VectorConfig -> Sequence[CandidateSource]` builder
    the composition root closes over its own `itop` to make — called fresh on
    every `find()`, never memoized, which is what keeps a family added or
    removed from the saved config live without a restart. This module does
    not import `content_sources.registry` itself; `build_sources` is how it
    reaches the same builder without knowing where it lives. `sources` stays
    the separate injection point for tests: production always passes
    `build_sources` and no `sources` override, a test does the opposite.
    """

    def __init__(
        self,
        store: ChunkStore,
        config: ConfigStore,
        build_sources: Callable[[VectorConfig], Sequence[CandidateSource]],
        *,
        sources: Sequence[CandidateSource] | None = None,
    ) -> None:
        self._store = store
        self._config = config
        self._build_sources = build_sources
        self._sources = sources

    def _unavailable(self, vector_cfg: VectorConfig, embeddings_cfg: EmbeddingsConfig) -> str | None:
        """Why this deployment cannot search, or None when it can — the same
        three checks and messages `require_vector`/`require_embeddings`
        (`vector/router.py`) used to gate on."""
        if not self._store.configured:
            return "Vector store is not configured (qdrant_url is not set)"
        if not vector_cfg.enabled:
            return "Vector indexing is disabled (vector: enabled)"
        if not embeddings_cfg.base_url or not embeddings_cfg.model:
            return "Embeddings endpoint is not configured"
        return None

    async def available(self) -> bool:
        """Whether this deployment can search right now — the gate a module
        checks before offering the tool that calls `find()`."""
        vector_cfg = await self._config.get("vector", VectorConfig)
        embeddings_cfg = await self._config.get("embeddings", EmbeddingsConfig)
        return self._unavailable(vector_cfg, embeddings_cfg) is None

    async def find(self, query: SearchQuery, principal: Principal) -> SearchResult:
        """Objects most similar to `query.text` that `principal` may see.

        Best first, at most `top`. Returns no hits for empty text and for an
        index that has nothing to say — a caller with no results is a normal
        outcome here, not a failure. `stats` carries what the call saw, for
        the run journal (TASK-014); every scenario parameter has already been
        validated by `SearchQuery` itself.

        Raises `SearchUnavailable` when this deployment cannot search at all,
        and `UnknownFamily` when nothing is registered for `query.family` —
        both before an embeddings client is created, so a bad call costs no
        connection and no embedding.
        """
        vector_cfg = await self._config.get("vector", VectorConfig)
        embeddings_cfg = await self._config.get("embeddings", EmbeddingsConfig)
        unavailable = self._unavailable(vector_cfg, embeddings_cfg)
        if unavailable is not None:
            raise SearchUnavailable(unavailable)
        sources = {
            source.name: source
            for source in (self._sources if self._sources is not None else self._build_sources(vector_cfg))
        }
        source = sources.get(query.family)
        if source is None:
            raise UnknownFamily(query.family, list(sources))
        empty = SearchResult(hits=[], stats=FindStats(requested=query.candidates, found=0, dropped_by_resolve=0))
        if not query.text.strip():
            return empty
        embedder = EmbeddingsClient(embeddings_cfg)
        try:
            embedding = (await embedder.embed([query.text]))[0]
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
        finally:
            await embedder.aclose()
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
