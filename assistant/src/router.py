import logging
import os
from typing import Optional
from uuid import UUID, uuid4

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from state.ticket_state import StateUnavailableError, TicketStateManager

logger = logging.getLogger(__name__)

router = APIRouter()


class WebhookPayload(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: int = Field(..., description="ID of the object in iTop")
    class_name: str = Field(..., alias="class", description="Class name of the object in iTop")
    is_async: bool = Field(True, alias="async", description="Whether to process the request asynchronously")


def _create_itop_client():
    from itoptop import Itop

    return Itop(
        url=os.getenv("ITOP_URL", "http://localhost/webservices/rest.php"),
        version="1.3",
        auth_user=os.getenv("ITOP_USER"),
        auth_pwd=os.getenv("ITOP_PWD"),
        auth_token=os.getenv("ITOP_TOKEN"),
    )


def _create_state_manager() -> TicketStateManager:
    import redis.asyncio as aioredis

    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379")
    client = aioredis.from_url(redis_url, decode_responses=True)
    return TicketStateManager(client)


def _create_ai_checker():
    from agent import ITopInfoChecker

    return ITopInfoChecker(
        model_name=os.getenv("LLM_MODEL"),
        base_url=os.getenv("LLM_BASE_URL"),
        api_key=os.getenv("LLM_API_KEY"),
    )


def _extract_id(value) -> Optional[int]:
    """Extract integer ID from a plain value or an iTop linked-object dict {"id": 5, "name": "..."}."""
    if isinstance(value, dict):
        value = value.get("id", 0)
    return int(value) if value else None


async def process_webhook_logic(payload: WebhookPayload, processing_id: UUID) -> dict:
    """Core logic for processing a webhook."""
    prefix = f"[{str(processing_id)}] "
    object_label = f"{payload.class_name}::{payload.id}"
    itop = _create_itop_client()
    checker = _create_ai_checker()
    bot_user = os.getenv("ITOP_AI_USER")

    logger.debug(f"{prefix}Fetching {object_label}")
    obj_data = await itop.schema(payload.class_name).find({"id": payload.id})

    if not obj_data:
        raise HTTPException(status_code=404, detail=f"Object {object_label} not found in iTop")

    if payload.class_name in ["UserRequest", "Incident"]:
        service_desc = ""
        subcategory_desc = ""

        logger.debug(f"{prefix}Fetching related service data for {object_label}")
        service_id = _extract_id(obj_data.get("service_id"))
        if service_id:
            service_data = await itop.schema("Service").find({"id": service_id})
            if service_data:
                service_desc = service_data.get("description", "")

        subcategory_id = _extract_id(obj_data.get("servicesubcategory_id"))
        if subcategory_id:
            subcategory_data = await itop.schema("ServiceSubcategory").find({"id": subcategory_id})
            if subcategory_data:
                subcategory_desc = subcategory_data.get("description", "")

        try:
            missing_info = await checker.check_completeness(
                title=obj_data.get("title", ""),
                description=obj_data.get("description", ""),
                service_desc=service_desc,
                subcategory_desc=subcategory_desc,
            )

            if missing_info:
                logger.info(f"{prefix}AI found missing info for {object_label}: {missing_info}")
                await itop.schema(payload.class_name).update(
                    {"id": payload.id},
                    {"public_log": {"add_item": {"message": missing_info, "user_login": bot_user}}},
                )
                ticket_ref = obj_data.get("ref") or str(payload.id)
                try:
                    state_manager = _create_state_manager()
                    await state_manager.increment_rounds(ticket_ref)
                except StateUnavailableError as e:
                    logger.warning(f"{prefix}Could not increment rounds for {ticket_ref}: {e}")
                obj_data["ai_check_result"] = missing_info
            else:
                logger.info(f"{prefix}AI check passed for {object_label}")
                obj_data["ai_check_result"] = "OK"
        except Exception as ai_err:
            logger.error(f"{prefix}AI completeness check failed for {object_label}: {ai_err}")
            obj_data["ai_check_result"] = "Error"

    return obj_data


@router.post("/webhook")
async def handle_webhook(payload: WebhookPayload, background_tasks: BackgroundTasks):
    """Handle webhook from iTop when an object is created."""
    processing_id = uuid4()
    prefix = f"[{str(processing_id)}] "
    object_label = f"{payload.class_name}::{payload.id}"
    logger.debug(f"{prefix}Received webhook: {payload}")
    try:
        if payload.is_async:
            logger.info(f"{prefix}Processing webhook asynchronously for {object_label}")
            background_tasks.add_task(process_webhook_logic, payload, processing_id)
            return {
                "status": "accepted",
                "processing_id": str(processing_id),
                "message": "Webhook processing started in background",
            }

        logger.info(f"{prefix}Processing webhook synchronously for {object_label}")
        obj_data = await process_webhook_logic(payload, processing_id)
        return {"status": "success", "processing_id": str(processing_id), "data": obj_data}

    except Exception as e:
        logger.error(f"{prefix}Error processing webhook: {str(e)}", exc_info=True)
        if isinstance(e, HTTPException):
            raise e
        raise HTTPException(status_code=500, detail=str(e))
