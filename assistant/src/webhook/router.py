import asyncio
import logging
import secrets
from typing import Annotated, Literal, Optional
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel

from deps import AppDeps
from pipelines.registry import PipelineHandler
from webhook.models import WebhookPayload

logger = logging.getLogger(__name__)
router = APIRouter()

# Keep strong references so tasks are not garbage-collected mid-run.
_background_tasks: set[asyncio.Task] = set()


class WebhookResponse(BaseModel):
    status: Literal["accepted"]
    processing_id: Optional[UUID]


async def verify_webhook_token(request: Request, x_auth_token: Annotated[str | None, Header()] = None) -> None:
    token = request.app.state.deps.settings.webhook_token
    if token is None:
        return
    if x_auth_token is None or not secrets.compare_digest(x_auth_token, token.get_secret_value()):
        raise HTTPException(status_code=401, detail="Invalid or missing X-Auth-Token header")


async def _process_safely(
    module: str, handler: PipelineHandler, payload: WebhookPayload, processing_id: UUID, deps: AppDeps
) -> None:
    label = f"{payload.obj_class}::{payload.id}"
    await deps.journal.start(processing_id, ticket=label, event=str(payload.event), module=module)
    try:
        await handler(payload, processing_id, deps)
    except Exception as e:
        logger.exception(f"[{processing_id}] Processing failed for {label}")
        await deps.journal.finish(processing_id, "failed", error=f"{type(e).__name__}: {e}")
    else:
        await deps.journal.finish(processing_id, "done")


@router.post("/webhook", status_code=202, dependencies=[Depends(verify_webhook_token)])
async def receive_webhook(payload: WebhookPayload, request: Request) -> WebhookResponse:
    entry = request.app.state.registry.resolve_entry(payload.obj_class, payload.event)
    if entry is None:
        raise HTTPException(status_code=400, detail=f"Unsupported class/event: {payload.obj_class}/{payload.event}")
    module, handler = entry

    deps: AppDeps = request.app.state.deps
    processing_id = uuid4()
    logger.info(f"[{processing_id}] Accepted {payload.obj_class}::{payload.id} ({payload.event})")
    task = asyncio.create_task(_process_safely(module, handler, payload, processing_id, deps))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return WebhookResponse(status="accepted", processing_id=processing_id)
