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

The door answers per family, not only per deployment: a deployment may index
one corpus and not another, so `available(family)` is what a consumer asks
before offering a tool over that corpus, and `find()` refuses a family this
deployment does not index rather than answering from a collection the sweep
stopped refreshing. A family whose index is being rebuilt under a new
embeddings model reads the same way, for the same reason and one step
further: there the collection is not merely stale, it holds vectors the query
cannot be compared with at all.

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
from itop_ai_assistant.state.counters import Counter, DailyCounters
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

    Stays a `Protocol` where the indexer's disjoint slice does not
    (TASK-039): two closely related members describing one source versus a
    handful of unrelated dependencies of a sweep — rule 3.2's actual criterion
    is cohesion, not member count.
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
        counters: DailyCounters,
        *,
        sources: Sequence[CandidateSource] | None = None,
    ) -> None:
        self._store = store
        self._config = config
        self._build_sources = build_sources
        self._counters = counters
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

    def _sources_by_name(self, vector_cfg: VectorConfig) -> dict[str, CandidateSource]:
        """The registered sources, keyed by family. Built fresh on every call
        — a family added to or removed from the saved config has to be live
        without a restart, which a list collected once would break."""
        return {
            source.name: source
            for source in (self._sources if self._sources is not None else self._build_sources(vector_cfg))
        }

    async def _family_unavailable(
        self, vector_cfg: VectorConfig, embeddings_cfg: EmbeddingsConfig, family: str
    ) -> str | None:
        """Why this family is not searchable, or None when it is.

        Separate from `_unavailable` because it answers a different question —
        "is this corpus indexed" rather than "can this deployment search" — and
        because it has to be asked *after* the source lookup, so a family name
        nothing is registered for stays an `UnknownFamily` and not this.

        A family with no entry at all reads the same as a switched-off one:
        the sweep skips both (`use_cases/indexer.py`), so both leave a
        collection nothing refreshes behind.

        The third reason is the model itself. A query embedded by one model
        cannot be compared with an index built by another, so while a family's
        replacement version is being filled it has to read as unavailable
        rather than be answered from the version still active — which the
        backend would refuse anyway, but as a failure about vector widths
        somewhere inside a run rather than as a closed gate the consumer
        already knows how to handle. A family that has never been indexed is
        not this case: it has no fingerprint to disagree with, and an empty
        answer from an index still warming up is the honest one.
        """
        family_cfg = vector_cfg.families.get(family)
        if family_cfg is None:
            return f"Family {family!r} has no entry in the vector config (vector: families)"
        if not family_cfg.enabled:
            return f"Indexing is switched off for family {family!r} (vector: families.{family}.enabled)"
        meta = await self._store.active_meta(family)
        if meta is not None and (meta.model, meta.dim) != (embeddings_cfg.model, embeddings_cfg.dimension):
            return (
                f"Family {family!r} is being rebuilt: its index v{meta.version} was built with "
                f"({meta.model!r}, dim={meta.dim}) and the configured model is "
                f"({embeddings_cfg.model!r}, dim={embeddings_cfg.dimension}) — it answers again once the "
                f"sweep has rebuilt it"
            )
        return None

    async def available(self, family: str | None = None) -> bool:
        """Whether this deployment can search right now — the gate a module
        checks before offering the tool that calls `find()`.

        Without `family` the question is about the deployment: is there a
        store, is indexing on, is there an endpoint to embed a query at. With
        one it also asks whether that corpus is indexed at all — a deployment
        that indexes FAQ and not tickets can search one and not the other, and
        a module offering a tool over a family nobody indexes would be
        answering from a frozen collection.

        "Indexed" means every half `find()` checks: something is registered
        under that name, the config keeps it on, and the index that answers
        was built by the model configured now. They split into different
        exceptions there and into the same False here.

        The last of the three is the only one that has to ask the store, and a
        store that does not answer reads as False rather than raising. This is
        a gate a consumer calls *before* it does anything — intake calls it
        while assembling a run — so an error escaping here would make an
        unreachable Qdrant fail whole runs that would otherwise have gone on
        without the search. `find()` is where a broken store is allowed to
        say so.
        """
        vector_cfg = await self._config.get("vector", VectorConfig)
        embeddings_cfg = await self._config.get("embeddings", EmbeddingsConfig)
        if self._unavailable(vector_cfg, embeddings_cfg) is not None:
            return False
        if family is None:
            return True
        # The source lookup first, exactly as `find()` orders it: a family with
        # a config entry but nothing registered for it is what `find()` answers
        # `UnknownFamily` to, and a gate that skipped the lookup would let the
        # consumer offer a tool whose only outcome is that exception.
        if family not in self._sources_by_name(vector_cfg):
            return False
        try:
            return await self._family_unavailable(vector_cfg, embeddings_cfg, family) is None
        except Exception as e:
            logger.warning(f"vector search: family {family!r} treated as unavailable — the store did not answer: {e}")
            return False

    async def find(self, query: SearchQuery, principal: Principal) -> SearchResult:
        """Objects most similar to `query.text` that `principal` may see.

        Best first, at most `top`. Returns no hits for empty text and for an
        index that has nothing to say — a caller with no results is a normal
        outcome here, not a failure. `stats` carries what the call saw, for
        the run journal (TASK-014); every scenario parameter has already been
        validated by `SearchQuery` itself.

        Raises `SearchUnavailable` when this deployment cannot search at all
        or when `query.family` is not one it indexes, and `UnknownFamily`
        when nothing is registered for that name — all before an embeddings
        client is created, so a bad call costs no connection and no embedding.

        Also the one place a search is counted (REQ-009 R3): the vector layer
        justifies its complexity here or nowhere, and an installation with the
        layer on and no searches is the answer to that. Counted after the
        search happened, so the two exceptions count as nothing — they say the
        deployment cannot search, not that it looked and found nothing. Every
        empty outcome counts as both: no text, no hits, or every hit dropped
        by visibility.
        """
        result = await self._search(query, principal)
        await self._counters.bump(Counter.VECTOR_SEARCHES)
        if not result.hits:
            await self._counters.bump(Counter.VECTOR_SEARCHES_EMPTY)
        return result

    async def _search(self, query: SearchQuery, principal: Principal) -> SearchResult:
        """The search itself; `find` is this plus the counting."""
        vector_cfg = await self._config.get("vector", VectorConfig)
        embeddings_cfg = await self._config.get("embeddings", EmbeddingsConfig)
        unavailable = self._unavailable(vector_cfg, embeddings_cfg)
        if unavailable is not None:
            raise SearchUnavailable(unavailable)
        sources = self._sources_by_name(vector_cfg)
        source = sources.get(query.family)
        if source is None:
            raise UnknownFamily(query.family, list(sources))
        # After the lookup above, not folded into `_unavailable`: a typo in a
        # consumer's family setting has to keep answering "no such family, the
        # known ones are …" rather than "that family is not indexed".
        family_unavailable = await self._family_unavailable(vector_cfg, embeddings_cfg, query.family)
        if family_unavailable is not None:
            raise SearchUnavailable(family_unavailable)
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
        Capping to `top` happens in `_search()`, after this — here the count
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
