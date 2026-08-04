"""Vector store diagnostics and control API (mounted under /api — admin-token auth).

Status is a diagnostic, not a gate: every failure mode returns 200 with the
error inside, so the admin UI can always render the page.
"""

import logging

from fastapi import APIRouter, HTTPException, Request

from itop_ai_assistant.config import EmbeddingsConfig, VectorConfig
from itop_ai_assistant.deps import AppDeps
from itop_ai_assistant.pipelines.scheduler import PeriodicTasks
from itop_ai_assistant.vector.index import RECONCILE_SENTINEL, VectorIndex
from itop_ai_assistant.vector.indexer import SWEEP_TASK

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/vector")


@router.get("/status")
async def vector_status(request: Request) -> dict:
    deps: AppDeps = request.app.state.deps
    tasks: PeriodicTasks = request.app.state.tasks
    vector_cfg = await deps.config_store.get("vector", VectorConfig)
    embeddings_cfg = await deps.config_store.get("embeddings", EmbeddingsConfig)

    database: dict = {"configured": deps.vector_db.configured, "ok": None, "error": None}
    index_info: dict | None = None
    sync: dict | None = None
    last_reconcile = None
    reindex_pending = False
    runs: list[dict] = []
    if deps.vector_db.configured:
        index = VectorIndex(deps.vector_db)
        try:
            meta = await index.active_meta()
            database["ok"] = True
            if meta is not None:
                stats = await index.stats()
                # None when no embeddings model is configured to compare against
                fingerprint_match = (
                    (meta.model, meta.dim) == (embeddings_cfg.model, embeddings_cfg.dimension)
                    if embeddings_cfg.model
                    else None
                )
                index_info = {
                    "active_version": meta.version,
                    "model": meta.model,
                    "dim": meta.dim,
                    "fingerprint_match": fingerprint_match,
                    "rows": stats.rows if stats else 0,
                }
            sync = await index.list_cursors()
            last_reconcile = await index.get_cursor(RECONCILE_SENTINEL)
            reindex_pending = await index.reindex_pending()
            runs = await index.journal_recent(10)
        except Exception as e:  # Postgres down, tables missing (migrations never ran) …
            database["ok"] = False
            database["error"] = f"{type(e).__name__}: {e}"

    return {
        "enabled": vector_cfg.enabled,
        "embeddings_configured": bool(embeddings_cfg.base_url and embeddings_cfg.model),
        "database": database,
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

    The request is a row in Postgres, not a flag in this process — whichever
    replica wins the advisory lock acts on it; waking the local loop only makes
    it happen sooner here.
    """
    deps: AppDeps = request.app.state.deps
    tasks: PeriodicTasks = request.app.state.tasks
    if not deps.vector_db.configured:
        raise HTTPException(status_code=409, detail="Vector store is not configured (database_url is not set)")
    vector_cfg = await deps.config_store.get("vector", VectorConfig)
    if not vector_cfg.enabled:
        raise HTTPException(status_code=409, detail="Vector indexing is disabled (vector: enabled)")
    try:
        await VectorIndex(deps.vector_db).request_reindex()
    except Exception as e:  # Postgres down, tables missing (migrations never ran) …
        logger.warning(f"reindex request could not be stored: {e}")
        raise HTTPException(status_code=503, detail=f"Vector store is unavailable: {type(e).__name__}: {e}") from e
    tasks.wake(SWEEP_TASK)
    return {"status": "scheduled"}
