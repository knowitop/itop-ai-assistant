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
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, model_validator

from itop_ai_assistant.config import EmbeddingsConfig, VectorConfig
from itop_ai_assistant.deps import AppDeps
from itop_ai_assistant.pipelines.scheduler import PeriodicTasks
from itop_ai_assistant.principal import Principal
from itop_ai_assistant.vector.embedder import EmbeddingsClient
from itop_ai_assistant.vector.indexer import SWEEP_TASK
from itop_ai_assistant.vector.search import FindStats, ObjectHit, SimilarSearch
from itop_ai_assistant.vector.store import DateRange
from itop_ai_assistant.vector_sources.registry import build_vector_sources
from itop_ai_assistant.vector_sources.tickets import FAMILY as TICKETS_FAMILY

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/vector")


async def require_vector(request: Request) -> VectorConfig:
    """The two gates every acting endpoint shares — and the config it needs anyway.

    Local to this router on purpose (`api_deps.py` is for what several entry
    points need). Declared as a `Depends` parameter rather than called from the
    body so the set of preconditions is visible in the signature: that
    `/reindex` asks for one gate and `/search` for two is a real difference —
    a backfill request survives an unconfigured embeddings endpoint, since it
    is a flag waiting for whichever pass eventually runs.
    """
    deps: AppDeps = request.app.state.deps
    if not deps.vector_store.configured:
        raise HTTPException(status_code=409, detail="Vector store is not configured (qdrant_url is not set)")
    vector_cfg = await deps.config_store.get("vector", VectorConfig)
    if not vector_cfg.enabled:
        raise HTTPException(status_code=409, detail="Vector indexing is disabled (vector: enabled)")
    return vector_cfg


async def require_embeddings(request: Request) -> EmbeddingsConfig:
    """Refuse anything that would have to embed text with no endpoint to embed it at."""
    deps: AppDeps = request.app.state.deps
    embeddings_cfg = await deps.config_store.get("embeddings", EmbeddingsConfig)
    if not embeddings_cfg.base_url or not embeddings_cfg.model:
        raise HTTPException(status_code=409, detail="Embeddings endpoint is not configured")
    return embeddings_cfg


@router.get("/status")
async def vector_status(request: Request) -> dict:
    deps: AppDeps = request.app.state.deps
    tasks: PeriodicTasks = request.app.state.tasks
    vector_cfg = await deps.config_store.get("vector", VectorConfig)
    embeddings_cfg = await deps.config_store.get("embeddings", EmbeddingsConfig)

    store_status: dict = {"configured": deps.vector_store.configured, "ok": None, "error": None}
    index_info: list[dict] | None = None
    sync: dict | None = None
    last_reconcile = None
    reindex_pending = False
    runs: list[dict] = []
    if deps.vector_store.configured:
        try:
            # Union of what code registers today and what Qdrant actually has
            # (TASK-008): a family dropped from the registry stays visible
            # here — `configured: false` — until its collection is dropped,
            # instead of silently disappearing from observability.
            configured_families = {s.name for s in build_vector_sources(deps, vector_cfg)}
            known_families = set(await deps.vector_store.list_families())
            store_status["ok"] = True
            index_info = []
            for family in sorted(configured_families | known_families):
                meta = await deps.vector_store.active_meta(family)
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
                    stats = await deps.vector_store.stats(family)
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
            sync = await deps.vector_sync.list_cursors()
            last_reconcile = await deps.vector_sync.get_reconcile()
            reindex_pending = await deps.vector_sync.reindex_pending()
            runs = await deps.vector_journal.recent(10)
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
async def vector_sources(request: Request) -> dict:
    """The chunking vocabulary of every registered source — what the admin UI
    renders its fragment editor from (ADR-018).

    Deliberately independent of Qdrant and of the embeddings endpoint:
    indexing must be configurable before — or without — either being up, so
    this never reports an infrastructure error, only what the code declares.
    `prepare()` is not called: the declarations are static and iTop is not
    needed to read them.
    """
    deps: AppDeps = request.app.state.deps
    vector_cfg = await deps.config_store.get("vector", VectorConfig)
    return {
        "sources": [
            {
                "name": source.name,
                # As claimed today, i.e. derived from the saved config — a
                # class nobody claims has no vocabulary to offer (TASK-016
                # owns the class → source model).
                "classes": list(source.classes),
                "fields": list(source.fields),
                "fragments": [asdict(fragment) for fragment in source.fragments],
            }
            for source in build_vector_sources(deps, vector_cfg)
        ]
    }


@router.post("/reindex", status_code=202)
async def vector_reindex(request: Request, vector_cfg: Annotated[VectorConfig, Depends(require_vector)]) -> dict:
    """Schedule a full backfill: cursor reset + an immediate sweep tick.

    The request is a flag in Redis, not in this process — whichever replica
    wins the sweep lock acts on it; waking the local loop only makes it
    happen sooner here.
    """
    deps: AppDeps = request.app.state.deps
    tasks: PeriodicTasks = request.app.state.tasks
    try:
        await deps.vector_sync.request_reindex()
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
    """`vector.store.DateRange` over the wire — same two inclusive bounds.

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
    """Mirrors `SimilarSearch.__init__`'s `family` plus `find()`'s own
    parameters (`vector/search.py`) — one-to-one, for manual testing."""

    family: str = Field(
        description="Which collection family to search — one name from `vector_sources/registry.py` "
        "(e.g. 'tickets'). Fixed at `SimilarSearch` construction in production code; here it's just "
        "the family to probe. 404 if no registered `VectorSource` has this name.",
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
    request: Request,
    body: SearchRequest,
    vector_cfg: Annotated[VectorConfig, Depends(require_vector)],
    embeddings_cfg: Annotated[EmbeddingsConfig, Depends(require_embeddings)],
) -> SearchResponse:
    """Debug endpoint: run one `SimilarSearch.find_with_stats()` and return the hits.

    Resolves candidates under the service account (`VectorSource.prepare()`),
    not the caller's own iTop identity, unless `principal_token` is given
    (`.claude/rules/vector.md`: search returns candidates, resolved against a
    principal's own token in production callers) — R4 has no production
    caller yet (TASK-015), this is how its two layers get exercised against a
    real iTop before one exists.
    """
    deps: AppDeps = request.app.state.deps
    sources = {s.name: s for s in build_vector_sources(deps, vector_cfg)}
    source = sources.get(body.family)
    if source is None:
        raise HTTPException(status_code=404, detail=f"Unknown family {body.family!r}; known: {sorted(sources)}")
    await source.prepare()

    resolve = source.find_existing_ids
    filters = body.filters
    allowed_org_ids: list[str] | None = None
    if body.principal_token:
        if body.family != TICKETS_FAMILY:
            raise HTTPException(
                status_code=501,
                detail=f"principal_token is only supported for family {TICKETS_FAMILY!r} today (TASK-015)",
            )
        principal = Principal.delegated(body.principal_token, login="debug", name="debug")
        bundle = await deps.itop.for_principal(principal, comment="vector debug search (TASK-015)")
        resolve = bundle.ticket_repo.find_existing_ids
        allowed_org_ids = await bundle.access_repo.allowed_org_ids()
        if allowed_org_ids is not None and (filters is None or "org_id" not in filters):
            filters = {**(filters or {}), "org_id": allowed_org_ids}

    embedder = EmbeddingsClient(embeddings_cfg)
    try:
        search = SimilarSearch(deps.vector_store, embedder, resolve, family=body.family)
        hits, stats = await search.find_with_stats(
            body.text,
            classes=body.classes,
            chunk_kinds=body.chunk_kinds,
            filters=filters,
            visibilities=body.visibilities,
            exclude=body.exclude,
            created=body.created.to_domain() if body.created else None,
            updated=body.updated.to_domain() if body.updated else None,
            min_score=body.min_score,
            candidates=body.candidates,
            top=body.top,
        )
        return SearchResponse(hits=hits, stats=stats, allowed_org_ids=allowed_org_ids)
    finally:
        await embedder.aclose()
