"""Vector store diagnostics and control API (mounted under /api — admin-token auth).

Status is a diagnostic, not a gate: every failure mode returns 200 with the
error inside, so the admin UI can always render the page.
"""

import logging

from fastapi import APIRouter, HTTPException, Request

from itop_ai_assistant.config import EmbeddingsConfig, VectorConfig
from itop_ai_assistant.deps import AppDeps
from itop_ai_assistant.pipelines.scheduler import PeriodicTasks
from itop_ai_assistant.vector.indexer import SWEEP_TASK
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
