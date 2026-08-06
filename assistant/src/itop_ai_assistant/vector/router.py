"""Vector store diagnostics and control API (mounted under /api — admin-token auth).

Status is a diagnostic, not a gate: every failure mode returns 200 with the
error inside, so the admin UI can always render the page.
"""

import logging
from datetime import datetime

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from itop_ai_assistant.config import EmbeddingsConfig, VectorConfig
from itop_ai_assistant.deps import AppDeps
from itop_ai_assistant.pipelines.scheduler import PeriodicTasks
from itop_ai_assistant.vector.embedder import EmbeddingsClient
from itop_ai_assistant.vector.indexer import SWEEP_TASK
from itop_ai_assistant.vector.search import ObjectHit, SimilarSearch
from itop_ai_assistant.vector_sources.registry import build_vector_sources

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/vector")


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


@router.post("/reindex", status_code=202)
async def vector_reindex(request: Request) -> dict:
    """Schedule a full backfill: cursor reset + an immediate sweep tick.

    The request is a flag in Redis, not in this process — whichever replica
    wins the sweep lock acts on it; waking the local loop only makes it
    happen sooner here.
    """
    deps: AppDeps = request.app.state.deps
    tasks: PeriodicTasks = request.app.state.tasks
    if not deps.vector_store.configured:
        raise HTTPException(status_code=409, detail="Vector store is not configured (qdrant_url is not set)")
    vector_cfg = await deps.config_store.get("vector", VectorConfig)
    if not vector_cfg.enabled:
        raise HTTPException(status_code=409, detail="Vector indexing is disabled (vector: enabled)")
    try:
        await deps.vector_sync.request_reindex()
    except Exception as e:  # Redis down …
        logger.warning(f"reindex request could not be stored: {e}")
        raise HTTPException(status_code=503, detail=f"Vector store is unavailable: {type(e).__name__}: {e}") from e
    tasks.wake(SWEEP_TASK)
    return {"status": "scheduled"}


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
    exclude: tuple[str, int] | None = Field(
        default=None,
        description="One (obj_class, obj_id) pair to drop from the results — typically the ticket the "
        "search is being run for, so it doesn't show up as similar to itself.",
    )
    updated_after: datetime | None = Field(
        default=None,
        description="Only consider chunks whose source object was updated at or after this timestamp. "
        "None applies no lower bound.",
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


@router.post("/search")
async def vector_search(request: Request, body: SearchRequest) -> list[ObjectHit]:
    """Debug endpoint: run one `SimilarSearch.find()` and return the hits.

    Resolves candidates under the service account (`VectorSource.prepare()`),
    not the caller's own iTop identity — results reflect what the index and
    the service account can see, not what any particular operator could see
    through `/webhook` (`.claude/rules/vector.md`: search returns candidates,
    resolved against a principal's own token in production callers).
    """
    deps: AppDeps = request.app.state.deps
    if not deps.vector_store.configured:
        raise HTTPException(status_code=409, detail="Vector store is not configured (qdrant_url is not set)")
    vector_cfg = await deps.config_store.get("vector", VectorConfig)
    if not vector_cfg.enabled:
        raise HTTPException(status_code=409, detail="Vector indexing is disabled (vector: enabled)")
    embeddings_cfg = await deps.config_store.get("embeddings", EmbeddingsConfig)
    if not embeddings_cfg.base_url or not embeddings_cfg.model:
        raise HTTPException(status_code=409, detail="Embeddings endpoint is not configured")

    sources = {s.name: s for s in build_vector_sources(deps, vector_cfg)}
    source = sources.get(body.family)
    if source is None:
        raise HTTPException(status_code=404, detail=f"Unknown family {body.family!r}; known: {sorted(sources)}")
    await source.prepare()

    embedder = EmbeddingsClient(embeddings_cfg)
    try:
        search = SimilarSearch(deps.vector_store, embedder, source.find_existing_ids, family=body.family)
        return await search.find(
            body.text,
            classes=body.classes,
            filters=body.filters,
            visibilities=body.visibilities,
            exclude=body.exclude,
            updated_after=body.updated_after,
            min_score=body.min_score,
            candidates=body.candidates,
            top=body.top,
        )
    finally:
        await embedder.aclose()
