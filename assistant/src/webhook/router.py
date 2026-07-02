import asyncio
import logging
import secrets
from typing import Annotated, Literal, Optional
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel

from config import get_settings
from webhook.handler import process_webhook_logic
from webhook.models import WebhookPayload

logger = logging.getLogger(__name__)
router = APIRouter()

SUPPORTED_CLASSES = {"UserRequest", "Incident"}

# Keep strong references so tasks are not garbage-collected mid-run.
_background_tasks: set[asyncio.Task] = set()


class WebhookResponse(BaseModel):
    status: Literal["accepted"]
    processing_id: Optional[UUID]


async def verify_webhook_token(x_auth_token: Annotated[str | None, Header()] = None) -> None:
    token = get_settings().webhook_token
    if token is None:
        return
    if x_auth_token is None or not secrets.compare_digest(x_auth_token, token.get_secret_value()):
        raise HTTPException(status_code=401, detail="Invalid or missing X-Auth-Token header")


async def _process_safely(payload: WebhookPayload, processing_id: UUID) -> None:
    try:
        await process_webhook_logic(payload=payload, processing_id=processing_id)
    except Exception:
        logger.exception(f"[{processing_id}] Processing failed for {payload.obj_class}::{payload.id}")


@router.post("/webhook", status_code=202, dependencies=[Depends(verify_webhook_token)])
async def receive_webhook(payload: WebhookPayload) -> WebhookResponse:
    if payload.obj_class not in SUPPORTED_CLASSES:
        raise HTTPException(status_code=400, detail=f"Unsupported class: {payload.obj_class}")

    processing_id = uuid4()
    logger.info(f"[{processing_id}] Accepted {payload.obj_class}::{payload.id}")
    task = asyncio.create_task(_process_safely(payload, processing_id))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return WebhookResponse(status="accepted", processing_id=processing_id)
