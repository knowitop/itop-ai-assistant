import asyncio
import logging
from typing import Literal, Optional
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from webhook.handler import process_webhook_logic
from webhook.models import WebhookPayload

logger = logging.getLogger(__name__)
router = APIRouter()

SUPPORTED_CLASSES = {"UserRequest", "Incident"}


class WebhookResponse(BaseModel):
    status: Literal["accepted"]
    processing_id: Optional[UUID]


@router.post("/webhook", status_code=202)
async def receive_webhook(payload: WebhookPayload) -> WebhookResponse:
    if payload.obj_class not in SUPPORTED_CLASSES:
        raise HTTPException(status_code=400, detail=f"Unsupported class: {payload.obj_class}")

    processing_id = uuid4()
    logger.info(f"[{processing_id}] Accepted {payload.obj_class}::{payload.id}")
    asyncio.create_task(process_webhook_logic(payload=payload, processing_id=processing_id))
    return WebhookResponse(status="accepted", processing_id=processing_id)
