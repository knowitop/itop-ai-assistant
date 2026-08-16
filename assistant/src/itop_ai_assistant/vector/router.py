"""Vector store diagnostics and control API (mounted under /api — admin-token auth).

Status is a diagnostic, not a gate: every failure mode returns 200 with the
error inside, so the admin UI can always render the page.

The endpoints that *act* share their preconditions through `require_vector` /
`require_embeddings` below; the two that only read (`/status`, `/sources`)
deliberately have none.
"""

import logging
from dataclasses import asdict
from datetime import datetime
from typing import TYPE_CHECKING, Annotated, Protocol

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, model_validator

from itop_ai_assistant.config import EmbeddingsConfig, VectorConfig
from itop_ai_assistant.core.api_deps import get_config_store
from itop_ai_assistant.core.principal import Principal
from itop_ai_assistant.pipelines.scheduler import PeriodicTasks
from itop_ai_assistant.repositories.sets import RepositorySet
from itop_ai_assistant.settings.config_store import ConfigStore
from itop_ai_assistant.vector.adapters.embedder import EmbeddingsClient
from itop_ai_assistant.vector.ports.query import FindStats, ObjectHit, SearchQuery
from itop_ai_assistant.vector.ports.store import ChunkStore, DateRange
from itop_ai_assistant.vector.sources.registry import ItopRepos, build_vector_sources
from itop_ai_assistant.vector.sources.tickets import FAMILY as TICKETS_FAMILY
from itop_ai_assistant.vector.state.index_journal import IndexJournal
from itop_ai_assistant.vector.state.sync_state import VectorSyncState
from itop_ai_assistant.vector.use_cases.indexer import SWEEP_TASK
from itop_ai_assistant.vector.use_cases.search import SimilarSearch, UnknownFamily

# core/deps.py imports the vector facade (concrete adapters); the facade's own
# __init__ imports this module for `router` — a real import of `core.deps`
# here would deadlock on that cycle. `AppDeps` below is a local variable
# annotation, never evaluated at runtime (PEP 526), so type-checking only
# costs nothing. `core/api_deps.py` is safe to import from for real, unlike
# `core/deps.py` itself: it names `AppDeps` the same way, in local variable
# annotations only, so importing it never runs `core/deps.py` and never
# touches this cycle. What still can't move there is any provider whose
# *parameter or return* type is `AppDeps` itself (a real, eagerly-evaluated
# annotation) — `get_itop`/`get_vector_store`/`get_vector_sync`/
# `get_vector_journal` stay local for that reason, built from direct
# submodule imports (`ports.store`, `state.*`, `sources.registry`) instead.
if TYPE_CHECKING:
    from itop_ai_assistant.core.deps import AppDeps

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/vector")


class _RouterItop(ItopRepos, Protocol):
    """`ItopRepos` (`sources/registry.py`) plus `for_principal`, for `/search`'s
    `principal_token` branch, which resolves under an engineer's own iTop
    identity instead of the service account (TASK-015).

    Not `pipelines.ports.ItopAccess`, which has the same two methods: that
    file names `itop_ai_assistant.vector` for `ChunkStore`, so importing it
    here would run this module while the vector facade is still mid-import —
    the cycle explained above, one hop further out. TASK-029 discusses this
    same duplication for `ItopRepos` itself.
    """

    async def for_principal(self, principal: Principal, *, comment: str) -> RepositorySet: ...


def get_itop(request: Request) -> _RouterItop:
    deps: AppDeps = request.app.state.deps
    return deps.itop


def get_vector_store(request: Request) -> ChunkStore:
    deps: AppDeps = request.app.state.deps
    return deps.vector_store


def get_vector_sync(request: Request) -> VectorSyncState:
    deps: AppDeps = request.app.state.deps
    return deps.vector_sync


def get_vector_journal(request: Request) -> IndexJournal:
    deps: AppDeps = request.app.state.deps
    return deps.vector_journal


async def get_vector_config(config_store: Annotated[ConfigStore, Depends(get_config_store)]) -> VectorConfig:
    return await config_store.get("vector", VectorConfig)


async def get_embeddings_config(
    config_store: Annotated[ConfigStore, Depends(get_config_store)],
) -> EmbeddingsConfig:
    return await config_store.get("embeddings", EmbeddingsConfig)


async def require_vector(
    vector_cfg: Annotated[VectorConfig, Depends(get_vector_config)],
    vector_store: Annotated[ChunkStore, Depends(get_vector_store)],
) -> VectorConfig:
    """The two gates every acting endpoint shares — and the config it needs anyway.

    Local to this router on purpose (`core/api_deps.py` is for what several entry
    points need). Declared as a `Depends` parameter rather than called from the
    body so the set of preconditions is visible in the signature: that
    `/reindex` asks for one gate and `/search` for two is a real difference —
    a backfill request survives an unconfigured embeddings endpoint, since it
    is a flag waiting for whichever pass eventually runs.
    """
    if not vector_store.configured:
        raise HTTPException(status_code=409, detail="Vector store is not configured (qdrant_url is not set)")
    if not vector_cfg.enabled:
        raise HTTPException(status_code=409, detail="Vector indexing is disabled (vector: enabled)")
    return vector_cfg


async def require_embeddings(
    embeddings_cfg: Annotated[EmbeddingsConfig, Depends(get_embeddings_config)],
) -> EmbeddingsConfig:
    """Refuse anything that would have to embed text with no endpoint to embed it at."""
    if not embeddings_cfg.base_url or not embeddings_cfg.model:
        raise HTTPException(status_code=409, detail="Embeddings endpoint is not configured")
    return embeddings_cfg


@router.get("/status")
async def vector_status(
    request: Request,
    vector_cfg: Annotated[VectorConfig, Depends(get_vector_config)],
    embeddings_cfg: Annotated[EmbeddingsConfig, Depends(get_embeddings_config)],
    vector_store: Annotated[ChunkStore, Depends(get_vector_store)],
    vector_sync: Annotated[VectorSyncState, Depends(get_vector_sync)],
    vector_journal: Annotated[IndexJournal, Depends(get_vector_journal)],
    itop: Annotated[_RouterItop, Depends(get_itop)],
) -> dict:
    tasks: PeriodicTasks = request.app.state.tasks

    store_status: dict = {"configured": vector_store.configured, "ok": None, "error": None}
    index_info: list[dict] | None = None
    sync: dict | None = None
    last_reconcile = None
    reindex_pending = False
    runs: list[dict] = []
    if vector_store.configured:
        try:
            # Union of what code registers today and what Qdrant actually has
            # (TASK-008): a family dropped from the registry stays visible
            # here — `configured: false` — until its collection is dropped,
            # instead of silently disappearing from observability.
            configured_families = {s.name for s in build_vector_sources(itop, vector_cfg)}
            known_families = set(await vector_store.list_families())
            store_status["ok"] = True
            index_info = []
            for family in sorted(configured_families | known_families):
                meta = await vector_store.active_meta(family)
                entry: dict = {
                    "family": family,
                    "configured": family in configured_families,
                    "active_version": None,
                    "model": None,
                    "dim": None,
                    "fingerprint_match": None,
                    "rows": None,
                }
                if meta is not None:
                    stats = await vector_store.stats(family)
                    # None when no embeddings model is configured to compare against
                    fingerprint_match = (
                        (meta.model, meta.dim) == (embeddings_cfg.model, embeddings_cfg.dimension)
                        if embeddings_cfg.model
                        else None
                    )
                    entry.update(
                        active_version=meta.version,
                        model=meta.model,
                        dim=meta.dim,
                        fingerprint_match=fingerprint_match,
                        rows=stats.rows if stats else 0,
                    )
                index_info.append(entry)
            sync = await vector_sync.list_cursors()
            last_reconcile = await vector_sync.get_reconcile()
            reindex_pending = await vector_sync.reindex_pending()
            runs = await vector_journal.recent(10)
        except Exception as e:  # backend down, not provisioned yet …
            store_status["ok"] = False
            store_status["error"] = f"{type(e).__name__}: {e}"

    return {
        "enabled": vector_cfg.enabled,
        "embeddings_configured": bool(embeddings_cfg.base_url and embeddings_cfg.model),
        "store": store_status,
        "index": index_info,
        "sync": sync,
        "last_reconcile": last_reconcile,
        "reindex_pending": reindex_pending,
        "runs": runs,
        "indexer_running": tasks.is_running(SWEEP_TASK),
    }


@router.get("/sources")
async def vector_sources(
    vector_cfg: Annotated[VectorConfig, Depends(get_vector_config)],
    itop: Annotated[_RouterItop, Depends(get_itop)],
) -> dict:
    """The chunking vocabulary of every registered source — what the admin UI
    renders its fragment editor from (ADR-018).

    Deliberately independent of Qdrant and of the embeddings endpoint:
    indexing must be configurable before — or without — either being up, so
    this never reports an infrastructure error, only what the code declares.
    `prepare()` is not called: the declarations are static and iTop is not
    needed to read them.
    """
    return {
        "sources": [
            {
                "name": source.name,
                # Every registered family is always present, whether or not it
                # currently has classes configured — recovering a class an
                # admin removed by mistake needs the family's vocabulary to
                # still be here, not just the classes still saved (TASK-021;
                # `vector/sources/registry.py::build_vector_sources`).
                "classes": list(source.classes),
                "fields": list(source.fields),
                "fragments": [asdict(fragment) for fragment in source.fragments],
            }
            for source in build_vector_sources(itop, vector_cfg)
        ]
    }


@router.post("/reindex", status_code=202)
async def vector_reindex(
    request: Request,
    vector_cfg: Annotated[VectorConfig, Depends(require_vector)],
    vector_sync: Annotated[VectorSyncState, Depends(get_vector_sync)],
) -> dict:
    """Schedule a full backfill: cursor reset + an immediate sweep tick.

    The request is a flag in Redis, not in this process — whichever replica
    wins the sweep lock acts on it; waking the local loop only makes it
    happen sooner here.
    """
    tasks: PeriodicTasks = request.app.state.tasks
    try:
        await vector_sync.request_reindex()
    except Exception as e:  # Redis down …
        logger.warning(f"reindex request could not be stored: {e}")
        raise HTTPException(status_code=503, detail=f"Vector store is unavailable: {type(e).__name__}: {e}") from e
    tasks.wake(SWEEP_TASK)
    return {"status": "scheduled"}


@router.post("/sweep", status_code=202)
async def vector_sweep(
    request: Request,
    vector_cfg: Annotated[VectorConfig, Depends(require_vector)],
    embeddings_cfg: Annotated[EmbeddingsConfig, Depends(require_embeddings)],
) -> dict:
    """Run the next incremental pass now instead of at the end of the wait.

    What it does *not* do is the difference from `/reindex`: no cursor reset,
    so the pass reads only what changed since the last one — the ordinary
    sweep, on demand.

    Waking a loop is local to this process, and there is nothing to persist:
    unlike the backfill flag, which any replica may act on, this schedules a
    tick here or nowhere at all — hence the 409 when this process has no sweep
    loop (registered at startup, so a store configured afterwards needs a
    restart). `require_embeddings` is in the signature for the same reason —
    unlike `/reindex`, which leaves a flag behind, a tick that would only skip
    is refused rather than reported as scheduled.

    A pending backfill stays pending — the woken tick reads the same Redis
    flag and runs as a backfill.
    """
    tasks: PeriodicTasks = request.app.state.tasks
    # Not a `Depends`: this one is about the state of *this process*, not the
    # configuration, and it has to be the last word — the config gates answer
    # first because their fix is the same everywhere, this one asks for a restart.
    if not tasks.wake(SWEEP_TASK):
        raise HTTPException(
            status_code=409,
            detail="The sweep loop is not registered in this process — it is put under the scheduler at "
            "startup, so a vector store configured afterwards needs a restart",
        )
    return {"status": "scheduled"}


class DateRangeBody(BaseModel):
    """`vector.ports.store.DateRange` over the wire — same two inclusive bounds.

    Validated here rather than deep in the store so an inverted or empty
    window is a 422 on parsing the body, not a 500 from inside the search.
    """

    after: datetime | None = Field(default=None, description="Lower bound, inclusive. Omit for an open lower side.")
    before: datetime | None = Field(default=None, description="Upper bound, inclusive. Omit for an open upper side.")

    @model_validator(mode="after")
    def _check(self) -> "DateRangeBody":
        self.to_domain()
        return self

    def to_domain(self) -> DateRange:
        return DateRange(after=self.after, before=self.before)


class SearchRequest(BaseModel):
    """Mirrors `SearchQuery` (`vector/ports/query.py`) one-to-one, for manual
    testing — plus `principal_token`, which is about *who* asks, not what for.

    Kept as its own transport model rather than exposing the value directly:
    the field descriptions below are documentation for whoever pokes this
    endpoint by hand, and they have no business travelling with the scenario
    into a run."""

    family: str = Field(
        description="Which collection family to search — one name from `vector/sources/registry.py` "
        "(e.g. 'tickets'). 404 if no registered `VectorSource` has this name.",
    )
    text: str = Field(description="Query text, embedded once and matched against the index by cosine similarity.")
    classes: list[str] | None = Field(
        default=None,
        description="Restrict to these `obj_class` values (e.g. ['UserRequest']). "
        "None (default) searches every class in the family.",
    )
    filters: dict[str, list[str]] | None = Field(
        default=None,
        description="Business filters applied during the index walk, keyed by the filter field name "
        "(e.g. {'status': ['resolved', 'closed']}). A key absent from the dict is unrestricted for that "
        "field; an empty list under a present key is rejected, not treated as 'no results'.",
    )
    visibilities: list[str] = Field(
        default=["public", "internal"],
        description="Chunk visibility levels to include — 'public' is caller-facing text, 'internal' is "
        "engineer-only notes.",
    )
    chunk_kinds: list[str] | None = Field(
        default=None,
        description="Restrict to these chunk kinds (e.g. ['profile', 'body'] vs. ['solution']) — a chunk's "
        "kind is part of its identity, not a business filter. None (default) matches any kind; an empty "
        "list is rejected, not treated as 'no results'.",
    )
    exclude: tuple[str, int] | None = Field(
        default=None,
        description="One (obj_class, obj_id) pair to drop from the results — typically the ticket the "
        "search is being run for, so it doesn't show up as similar to itself.",
    )
    created: DateRangeBody | None = Field(
        default=None,
        description="Window over the source object's creation date, both bounds inclusive and each "
        "optional ({'after': ...} alone is a plain lower bound). None applies no window at all. "
        "Note that for a source that reports no creation date this is the time the object was last "
        "written to the index, not when it came into being.",
    )
    updated: DateRangeBody | None = Field(
        default=None,
        description="Window over the source object's last modification, both bounds inclusive and each "
        "optional. None applies no window. An object indexed without an update date passes no window "
        "here, not even one made of a single upper bound.",
    )
    min_score: float | None = Field(
        default=None,
        description="Drop a candidate whose similarity score is below this value, regardless of its "
        "rank among the results. None (default) applies no floor.",
    )
    candidates: int = Field(
        default=15,
        description="How many nearest neighbours to pull from the index before resolving them against "
        "the source (the source may reject some — see `top`).",
    )
    top: int = Field(
        default=5,
        description="Max number of resolved hits to return, best-first. Kept lower than `candidates` "
        "because some candidates get dropped when the source no longer confirms them.",
    )
    principal_token: str | None = Field(
        default=None,
        description="An iTop personal/application token to resolve candidates under and to read the "
        "R4 org pre-filter from, instead of the service account (TASK-015) — paste an engineer's own "
        "token to check what `AccessRepository.allowed_org_ids()` returns for them and whether the "
        "org pre-filter and the source's own resolve agree (`stats.dropped_by_resolve`). When given, "
        "it also fills `filters['org_id']` unless the request already sets that key. Only supported "
        "for the 'tickets' family today — 501 otherwise.",
    )

    @model_validator(mode="after")
    def _check(self) -> "SearchRequest":
        """Same trick as `DateRangeBody`: build the value so its own rules
        (an empty list where the key should have been omitted, `top` above
        `candidates`) answer with 422 instead of a 500 from inside the
        handler."""
        self.to_query()
        return self

    def to_query(self, *, filters: dict[str, list[str]] | None = None) -> SearchQuery:
        """The scenario this request describes.

        `filters` overrides the body's own — the `principal_token` branch adds
        the org pre-filter to them before searching.
        """
        return SearchQuery(
            text=self.text,
            family=self.family,
            classes=self.classes,
            chunk_kinds=self.chunk_kinds,
            filters=self.filters if filters is None else filters,
            visibilities=self.visibilities,
            exclude=self.exclude,
            created=self.created.to_domain() if self.created else None,
            updated=self.updated.to_domain() if self.updated else None,
            min_score=self.min_score,
            candidates=self.candidates,
            top=self.top,
        )


class SearchResponse(BaseModel):
    hits: list[ObjectHit]
    stats: FindStats
    allowed_org_ids: list[str] | None = Field(
        default=None,
        description="Set only when `principal_token` was given: that principal's iTop 'Allowed "
        "Organizations', or None meaning iTop itself reports no restriction (empty list).",
    )


@router.post("/search")
async def vector_search(
    body: SearchRequest,
    vector_cfg: Annotated[VectorConfig, Depends(require_vector)],
    embeddings_cfg: Annotated[EmbeddingsConfig, Depends(require_embeddings)],
    vector_store: Annotated[ChunkStore, Depends(get_vector_store)],
    itop: Annotated[_RouterItop, Depends(get_itop)],
) -> SearchResponse:
    """Debug endpoint: run one `SimilarSearch.find()` and return the hits.

    Confirms candidates under the service account unless `principal_token` is
    given (`.claude/rules/vector.md`: search returns candidates, confirmed
    against a principal's own token) — R4 has no production caller yet
    (TASK-015), this is how its two layers get exercised against a real iTop
    before one exists.

    What this handler still does by hand is R4's *layer 1*: reading the
    principal's allowed organizations and turning them into a pre-filter.
    That stayed here deliberately (TASK-032) — it shapes the walk before it
    starts, it is over-permissive by design (ADR-003), and computing it means
    knowing what an organization is, which is the caller's language, not the
    subsystem's. Layer 2, the one confidentiality actually rests on, is no
    longer assembled here at all.
    """
    filters = body.filters
    principal = Principal.service()
    allowed_org_ids: list[str] | None = None
    if body.principal_token:
        if body.family != TICKETS_FAMILY:
            raise HTTPException(
                status_code=501,
                detail=f"principal_token is only supported for family {TICKETS_FAMILY!r} today (TASK-015)",
            )
        principal = Principal.delegated(body.principal_token, login="debug", name="debug")
        repos = await itop.for_principal(principal, comment="vector debug search (TASK-015)")
        allowed_org_ids = await repos.access_repo.allowed_org_ids()
        if allowed_org_ids is not None and (filters is None or "org_id" not in filters):
            filters = {**(filters or {}), "org_id": allowed_org_ids}

    embedder = EmbeddingsClient(embeddings_cfg)
    try:
        search = SimilarSearch(vector_store, embedder, itop, vector_cfg)
        result = await search.find(body.to_query(filters=filters), principal)
        return SearchResponse(hits=result.hits, stats=result.stats, allowed_org_ids=allowed_org_ids)
    except UnknownFamily as unknown:
        raise HTTPException(status_code=404, detail=str(unknown)) from unknown
    finally:
        await embedder.aclose()
