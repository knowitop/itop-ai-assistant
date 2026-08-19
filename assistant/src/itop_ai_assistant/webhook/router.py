import asyncio
import logging
from typing import Annotated, Literal, Optional
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from itop_ai_assistant.core.api_deps import require_configured, resolve_principal, verify_webhook_token
from itop_ai_assistant.core.deps import AppDeps, get_deps
from itop_ai_assistant.pipelines.context import RunContext
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


async def _process_safely(handler: WebhookHandler, payload: WebhookPayload, run: RunContext, deps: AppDeps) -> None:
    """Run a webhook trigger in the background. Nobody is waiting for an answer,
    so a failure is recorded and logged, never raised."""
    try:
        async with journalled_run(
            deps.journal,
            run,
            kind="webhook",
            subject=str(payload),
            event=str(payload.event),
        ):
            await handler(payload, run, deps)
    except Exception:
        logger.exception(f"[{run.processing_id}] Processing failed for {payload}")


@router.post("/webhook", status_code=202, dependencies=[Depends(verify_webhook_token), Depends(require_configured)])
async def receive_webhook(
    payload: WebhookPayload,
    request: Request,
    deps: Annotated[AppDeps, Depends(get_deps)],
) -> WebhookResponse:
    entry = request.app.state.registry.resolve_webhook(payload.obj_class, payload.event)
    if entry is None:
        raise HTTPException(status_code=400, detail=f"Unsupported class/event: {payload.obj_class}/{payload.event}")
    module, handler = entry

    run = RunContext(processing_id=uuid4(), module=module, principal=await resolve_principal(request))
    logger.info(f"[{run.processing_id}] Accepted {payload} ({payload.event})")
    task = asyncio.create_task(_process_safely(handler, payload, run, deps))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return WebhookResponse(status="accepted", processing_id=run.processing_id)
