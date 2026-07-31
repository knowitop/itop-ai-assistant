import asyncio
import logging
from typing import Literal, Optional
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from itop_ai_assistant.api_deps import require_configured, verify_webhook_token
from itop_ai_assistant.deps import AppDeps
from itop_ai_assistant.pipelines.registry import WebhookHandler
from itop_ai_assistant.pipelines.runner import journalled_run
from itop_ai_assistant.webhook.models import WebhookPayload

logger = logging.getLogger(__name__)
router = APIRouter()

# Keep strong references so tasks are not garbage-collected mid-run.
_background_tasks: set[asyncio.Task] = set()


class WebhookResponse(BaseModel):
    status: Literal["accepted"]
    processing_id: Optional[UUID]


async def _process_safely(
    module: str, handler: WebhookHandler, payload: WebhookPayload, processing_id: UUID, deps: AppDeps
) -> None:
    """Run a webhook trigger in the background. Nobody is waiting for an answer,
    so a failure is recorded and logged, never raised."""
    try:
        async with journalled_run(
            deps,
            processing_id,
            kind="webhook",
            subject=payload.label,
            event=str(payload.event),
            module=module,
        ):
            await handler(payload, processing_id, deps)
    except Exception:
        logger.exception(f"[{processing_id}] Processing failed for {payload.label}")


@router.post("/webhook", status_code=202, dependencies=[Depends(verify_webhook_token), Depends(require_configured)])
async def receive_webhook(payload: WebhookPayload, request: Request) -> WebhookResponse:
    entry = request.app.state.registry.resolve_webhook(payload.obj_class, payload.event)
    if entry is None:
        raise HTTPException(status_code=400, detail=f"Unsupported class/event: {payload.obj_class}/{payload.event}")
    module, handler = entry

    deps: AppDeps = request.app.state.deps
    processing_id = uuid4()
    logger.info(f"[{processing_id}] Accepted {payload.label} ({payload.event})")
    task = asyncio.create_task(_process_safely(module, handler, payload, processing_id, deps))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return WebhookResponse(status="accepted", processing_id=processing_id)
