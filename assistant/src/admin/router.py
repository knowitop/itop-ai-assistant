"""Admin API: configuration, prompts and processing-run monitoring.

Backend for the future admin UI. Protected by the `security.admin_token`
runtime setting (standard bearer auth: `Authorization: Bearer <token>`),
separate from the webhook token.
"""

import logging
import secrets
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ValidationError

from admin.setup import router as setup_router
from config import SecurityConfig
from deps import AppDeps
from journal import ProcessingRun
from pipelines.registry import ModuleInfo
from prompt_store import PromptStoreError
from vector.router import router as vector_router

logger = logging.getLogger(__name__)

# auto_error=False: a missing header must fall through to our own check —
# the API is open until an admin token is set (first-run mode)
_bearer = HTTPBearer(auto_error=False)


async def verify_admin_token(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)] = None,
) -> None:
    deps: AppDeps = request.app.state.deps
    security = await deps.config_store.get("security", SecurityConfig)
    if security.admin_token is None:
        # First-run mode: the API stays open until the wizard sets a token
        return
    if credentials is None or not secrets.compare_digest(credentials.credentials, security.admin_token):
        raise HTTPException(
            status_code=401,
            detail="Invalid or missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )


router = APIRouter(prefix="/api", dependencies=[Depends(verify_admin_token)])
router.include_router(setup_router)
router.include_router(vector_router)


def _deps(request: Request) -> AppDeps:
    return request.app.state.deps


def _module_or_404(request: Request, module: str) -> ModuleInfo:
    info = request.app.state.registry.get_module(module)
    if info is None:
        raise HTTPException(status_code=404, detail=f"Unknown module: {module}")
    return info


@router.get("/modules")
async def list_modules(request: Request) -> list[dict]:
    return [
        {
            "name": m.name,
            "description": m.description,
            "has_config": m.config_model is not None,
            "prompts": list(m.prompt_names),
        }
        for m in request.app.state.registry.modules
    ]


@router.get("/config/{module}")
async def get_config(module: str, request: Request) -> dict:
    info = _module_or_404(request, module)
    if info.config_model is None:
        raise HTTPException(status_code=404, detail=f"Module {module!r} has no config")
    cfg = await _deps(request).config_store.get(module, info.config_model)
    return cfg.model_dump()


@router.get("/config/{module}/schema")
async def get_config_schema(module: str, request: Request) -> dict:
    info = _module_or_404(request, module)
    if info.config_model is None:
        raise HTTPException(status_code=404, detail=f"Module {module!r} has no config")
    return info.config_model.model_json_schema()


@router.put("/config/{module}")
async def update_config(module: str, body: dict[str, Any], request: Request) -> dict:
    info = _module_or_404(request, module)
    if info.config_model is None:
        raise HTTPException(status_code=404, detail=f"Module {module!r} has no config")
    try:
        cfg = await _deps(request).config_store.set(module, body, info.config_model)
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    logger.info(f"Config for module {module!r} updated via admin API")
    return cfg.model_dump()


@router.delete("/config/{module}", status_code=204)
async def reset_config(module: str, request: Request) -> None:
    _module_or_404(request, module)
    await _deps(request).config_store.reset(module)
    logger.info(f"Config for module {module!r} reset to defaults via admin API")


@router.get("/prompts/{module}")
async def get_prompts(module: str, request: Request) -> dict:
    _module_or_404(request, module)
    store = _deps(request).prompt_store
    prompts = await store.get(module)
    overridden = await store.overrides(module)
    return {"prompts": prompts, "overridden": sorted(overridden)}


class PromptUpdate(BaseModel):
    text: str


@router.put("/prompts/{module}/{name}")
async def update_prompt(module: str, name: str, body: PromptUpdate, request: Request) -> dict:
    info = _module_or_404(request, module)
    store = _deps(request).prompt_store

    prompts = await store.get(module)
    if name not in prompts:
        raise HTTPException(status_code=404, detail=f"Unknown prompt {name!r}. Known: {sorted(prompts)}")

    if info.validate_prompts is not None:
        try:
            info.validate_prompts({**prompts, name: body.text})
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e)) from e

    try:
        await store.set(module, name, body.text)
    except PromptStoreError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    logger.info(f"Prompt {module}/{name} updated via admin API")
    return {"name": name, "text": body.text}


@router.delete("/prompts/{module}/{name}", status_code=204)
async def reset_prompt(module: str, name: str, request: Request) -> None:
    _module_or_404(request, module)
    await _deps(request).prompt_store.reset(module, name)
    logger.info(f"Prompt {module}/{name} reset via admin API")


@router.get("/runs")
async def list_runs(
    request: Request,
    limit: int = 50,
    ticket: str | None = None,
    status: str | None = None,
) -> list[ProcessingRun]:
    return await _deps(request).journal.list(limit=limit, ticket=ticket, status=status)


@router.get("/runs/{processing_id}")
async def get_run(processing_id: str, request: Request) -> ProcessingRun:
    run = await _deps(request).journal.get(processing_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Run {processing_id} not found")
    return run
