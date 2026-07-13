"""Vector store diagnostics and control API (mounted under /api — admin-token auth).

Status is a diagnostic, not a gate: every failure mode returns 200 with the
error inside, so the admin UI can always render the page.
"""

import logging

from fastapi import APIRouter, HTTPException, Request

from config import EmbeddingsConfig, VectorConfig
from deps import AppDeps
from vector.index import RECONCILE_SENTINEL, VectorIndex

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/vector")


@router.get("/status")
async def vector_status(request: Request) -> dict:
    deps: AppDeps = request.app.state.deps
    indexer = getattr(request.app.state, "vector_indexer", None)
    vector_cfg = await deps.config_store.get("vector", VectorConfig)
    embeddings_cfg = await deps.config_store.get("embeddings", EmbeddingsConfig)

    database: dict = {"configured": deps.vector_db.configured, "ok": None, "error": None}
    index_info: dict | None = None
    sync: dict | None = None
    last_reconcile = None
    runs: list[dict] = []
    if deps.vector_db.configured:
        index = VectorIndex(deps.vector_db, env=vector_cfg.env)
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
                    "size_bytes": stats.size_bytes if stats else 0,
                }
            sync = await index.list_cursors()
            last_reconcile = await index.get_cursor(RECONCILE_SENTINEL)
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
        "runs": runs,
        "indexer_running": indexer is not None and indexer.running,
    }


@router.post("/reindex", status_code=202)
async def vector_reindex(request: Request) -> dict:
    """Schedule a full backfill: cursor reset + an immediate sweep tick."""
    deps: AppDeps = request.app.state.deps
    indexer = getattr(request.app.state, "vector_indexer", None)
    if indexer is None:
        raise HTTPException(status_code=409, detail="Vector store is not configured (database_url is not set)")
    vector_cfg = await deps.config_store.get("vector", VectorConfig)
    if not vector_cfg.enabled:
        raise HTTPException(status_code=409, detail="Vector indexing is disabled (vector: enabled)")
    indexer.request_reindex()
    return {"status": "scheduled"}
