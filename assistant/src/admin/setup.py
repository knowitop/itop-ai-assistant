"""Setup API: runtime connection configuration — backend for the setup wizard.

Connection sections (itop, llm, security, ticket_mapping) are stored through
the same ConfigStore as module config (Redis overrides > env defaults), but
served by dedicated endpoints because secrets need special treatment:

- GET never returns secret values — only `secrets: {field: is_set}` flags;
- PATCH merges the body over the current *effective* config, so a field
  absent from the body keeps its value and an explicit null clears it.
"""

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ValidationError

from config import (
    EmbeddingsConfig,
    ItopConfig,
    LlmConfig,
    SecurityConfig,
    TicketMappingConfig,
    VectorConfig,
    missing_setup,
)
from deps import AppDeps, create_itop_client, create_llm
from graph.enrichment.nodes.utils import strip_thinking
from itop_provisioning import provision_itop
from vector.embedder import EmbeddingsClient

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/setup")

SETUP_SECTIONS: dict[str, type[BaseModel]] = {
    "itop": ItopConfig,
    "llm": LlmConfig,
    "security": SecurityConfig,
    "ticket_mapping": TicketMappingConfig,
    # Vector store (optional infrastructure — not part of missing_setup)
    "embeddings": EmbeddingsConfig,
    "vector": VectorConfig,
}

_TEST_TIMEOUT = 30.0  # seconds; keeps connection tests from hanging the wizard
_PROVISION_TIMEOUT = 60.0  # seconds; provisioning makes ~10 sequential iTop requests


def _deps(request: Request) -> AppDeps:
    return request.app.state.deps


def _model_or_404(section: str) -> type[BaseModel]:
    model = SETUP_SECTIONS.get(section)
    if model is None:
        raise HTTPException(
            status_code=404, detail=f"Unknown setup section: {section}. Known: {sorted(SETUP_SECTIONS)}"
        )
    return model


def _masked(cfg: BaseModel) -> dict:
    """Section values safe to return to the UI: secrets replaced by is-set flags."""
    values = cfg.model_dump()
    secrets_state = {}
    for field in getattr(type(cfg), "SECRET_FIELDS", frozenset()):
        secrets_state[field] = bool(values.pop(field))
    return {"values": values, "secrets": secrets_state}


async def _merged_with_current(request: Request, section: str, model: type[BaseModel], body: dict[str, Any]) -> dict:
    """Body merged over the current effective config.

    Absent field = keep current value (secrets survive UI round-trips),
    explicit null = clear.
    """
    current = await _deps(request).config_store.get(section, model)
    return {**current.model_dump(), **body}


@router.get("/status")
async def setup_status(request: Request) -> dict:
    store = _deps(request).config_store
    itop_cfg = await store.get("itop", ItopConfig)
    llm_cfg = await store.get("llm", LlmConfig)
    security_cfg = await store.get("security", SecurityConfig)
    embeddings_cfg = await store.get("embeddings", EmbeddingsConfig)
    missing = missing_setup(itop_cfg, llm_cfg)
    return {
        "configured": not missing,
        "missing": missing,
        "sections": {
            "itop": _masked(itop_cfg),
            "llm": _masked(llm_cfg),
            "security": _masked(security_cfg),
            "embeddings": _masked(embeddings_cfg),
        },
    }


@router.get("/{section}")
async def get_section(section: str, request: Request) -> dict:
    model = _model_or_404(section)
    cfg = await _deps(request).config_store.get(section, model)
    return _masked(cfg)


@router.patch("/{section}")
async def update_section(section: str, body: dict[str, Any], request: Request) -> dict:
    model = _model_or_404(section)
    values = await _merged_with_current(request, section, model, body)
    try:
        cfg = await _deps(request).config_store.set(section, values, model)
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    logger.info(f"Setup section {section!r} updated via admin API")
    return _masked(cfg)


@router.delete("/{section}", status_code=204)
async def reset_section(section: str, request: Request) -> None:
    _model_or_404(section)
    await _deps(request).config_store.reset(section)
    logger.info(f"Setup section {section!r} reset to env defaults via admin API")


@router.post("/test-itop")
async def test_itop(request: Request, body: dict[str, Any] | None = None) -> dict:
    """Probe the iTop connection: auth + REST profile + AI service account.

    Body fields override the stored config for this probe only — nothing is
    saved. Secrets absent from the body are taken from the stored config, so
    the UI can re-test without re-entering the password.
    """
    values = await _merged_with_current(request, "itop", ItopConfig, body or {})
    try:
        cfg = ItopConfig(**values)
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    if not cfg.url:
        return {"ok": False, "error": "No URL: set the iTop REST API URL first"}
    if not cfg.has_auth:
        return {"ok": False, "error": "No credentials: set user+pwd or token"}

    client = create_itop_client(cfg)
    try:
        # Resolves the Person behind the credentials — fails on bad auth or a
        # missing "REST Services User" profile, exactly what the wizard checks.
        person = await asyncio.wait_for(
            client.schema("Person").find_one({"id": ("=", ":current_contact_id")}),
            timeout=_TEST_TIMEOUT,
        )
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    finally:
        await client.aclose()
    if person is None:
        return {"ok": False, "error": "Authenticated, but no Person is linked to the account"}
    return {"ok": True, "ai_person": person.get("friendlyname")}


@router.post("/provision-itop")
async def provision_itop_endpoint(request: Request, body: dict[str, Any]) -> dict:
    """Create the iTop-side triggers and webhooks (see itop_provisioning).

    Requires one-time admin credentials in the body (`user`+`pwd` or `token`)
    — they are used for these requests only and are never stored. `url`
    defaults to the saved itop section; the webhook token comes from the
    saved security section.
    """
    deps = _deps(request)
    security = await deps.config_store.get("security", SecurityConfig)
    if not security.webhook_token:
        return {"ok": False, "error": "Set the webhook token first (security section)"}
    backend_url = str(body.get("backend_url") or "").strip()
    if not backend_url:
        return {"ok": False, "error": "backend_url is required"}

    stored = await deps.config_store.get("itop", ItopConfig)
    try:
        cfg = ItopConfig(
            url=str(body.get("url") or stored.url),
            api_version=stored.api_version,
            timeout=stored.timeout,
            user=body.get("user"),
            pwd=body.get("pwd"),
            token=body.get("token"),
        )
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    if not cfg.has_auth:
        return {"ok": False, "error": "No admin credentials: provide user+pwd or token"}

    client = create_itop_client(cfg)
    try:
        report = await asyncio.wait_for(
            provision_itop(client, backend_url, security.webhook_token), timeout=_PROVISION_TIMEOUT
        )
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    finally:
        await client.aclose()
    logger.info(f"iTop provisioning finished: {[(r['status'], r['name']) for r in report]}")
    return {"ok": True, "report": report}


@router.post("/test-llm")
async def test_llm(request: Request, body: dict[str, Any] | None = None) -> dict:
    """Probe the LLM endpoint with a one-word completion. Nothing is saved."""
    values = await _merged_with_current(request, "llm", LlmConfig, body or {})
    try:
        cfg = LlmConfig(**values)
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    if not cfg.base_url:
        return {"ok": False, "error": "No endpoint: set the LLM base URL first"}
    if not cfg.model:
        return {"ok": False, "error": "No model: set llm model first"}

    llm = create_llm(cfg)
    try:
        answer = await asyncio.wait_for(llm.ainvoke("Reply with a single word: OK"), timeout=_TEST_TIMEOUT)
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    text = strip_thinking(answer.content, tuple(cfg.think_tags)).strip()
    return {"ok": True, "model": cfg.model, "response": text[:200]}


@router.post("/test-embeddings")
async def test_embeddings(request: Request, body: dict[str, Any] | None = None) -> dict:
    """Probe the embeddings endpoint with one text. Nothing is saved.

    Measures the endpoint's real vector dimension (`embed_raw` skips the
    config check) so the wizard can flag a wrong `dimension` value via
    `dimension_match` instead of failing opaquely later.
    """
    values = await _merged_with_current(request, "embeddings", EmbeddingsConfig, body or {})
    try:
        cfg = EmbeddingsConfig(**values)
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    if not cfg.base_url:
        return {"ok": False, "error": "No endpoint: set the embeddings base URL first"}
    if not cfg.model:
        return {"ok": False, "error": "No model: set embeddings model first"}

    client = EmbeddingsClient(cfg)
    try:
        vectors = await asyncio.wait_for(client.embed_raw(["ping"]), timeout=_TEST_TIMEOUT)
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    finally:
        await client.aclose()
    dimension = len(vectors[0]) if vectors else 0
    return {
        "ok": True,
        "model": cfg.model,
        "dimension": dimension,
        "dimension_match": dimension == cfg.dimension,
    }
