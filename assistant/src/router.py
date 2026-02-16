import logging
import os
from uuid import UUID, uuid4

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)

router = APIRouter()


class WebhookPayload(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: int = Field(..., description="ID of the object in iTop")
    class_name: str = Field(..., alias="class", description="Class name of the object in iTop")
    is_async: bool = Field(True, alias="async", description="Whether to process the request asynchronously")


def _create_itop_client():
    # iTop Client initialization
    itop_url = os.getenv("ITOP_URL", "http://localhost/webservices/rest.php")
    itop_user = os.getenv("ITOP_USER")
    itop_pwd = os.getenv("ITOP_PWD")
    itop_token = os.getenv("ITOP_TOKEN")

    from itop.client import ITopClient

    return ITopClient(url=itop_url, auth_user=itop_user, auth_pwd=itop_pwd, auth_token=itop_token)


def _create_ai_checker():
    from agent import ITopInfoChecker

    llm_model_name = os.getenv("LLM_MODEL", "google_genai:gemini-2.5-flash-lite")
    return ITopInfoChecker(llm_model_name)


def get_itop_object(class_name: str, object_id: int, output_fields: list[str]) -> dict:
    """
    Fetch a single object from iTop and return its fields.
    """
    itop_client = _create_itop_client()

    logger.debug(f"Fetching object details for {class_name}::{object_id}")
    result = itop_client.get_objects(class_name=class_name, key=object_id, output_fields=output_fields)
    logger.debug(f"iTop API response for {class_name}::{object_id}: {result}")

    objects = result.get("objects")
    if not objects:
        logger.warning(f"Object {class_name}::{object_id} not found in iTop")
        return {}

    obj_key = list(objects.keys())[0]
    return objects[obj_key]["fields"]


async def process_webhook_logic(payload: WebhookPayload, processing_id: UUID) -> dict:
    """
    Core logic for processing a webhook.
    """
    prefix = f"[{str(processing_id)}] "
    object_label = f"{payload.class_name}::{payload.id}"
    itop_client = _create_itop_client()
    checker = _create_ai_checker()

    # Fetch detailed information about the main object
    obj_data = get_itop_object(
        class_name=payload.class_name,
        object_id=payload.id,
        output_fields=["ref", "title", "description", "service_id", "servicesubcategory_id"],
    )

    if not obj_data:
        raise HTTPException(status_code=404, detail=f"Object {object_label} not found in iTop")

    # If it's a UserRequest or Incident, try to fetch Service and ServiceSubcategory details
    if payload.class_name in ["UserRequest", "Incident"]:
        service_desc = ""
        subcategory_desc = ""

        logger.debug(f"{prefix}Fetching related service data if needed for {object_label}")
        service_id = obj_data.get("service_id")
        if service_id:
            service_data = get_itop_object(
                class_name="Service", object_id=int(service_id), output_fields=["name", "description"]
            )
            if service_data:
                obj_data["service_details"] = service_data
                service_desc = service_data.get("description", "")

        subcategory_id = obj_data.get("servicesubcategory_id")
        if subcategory_id:
            subcategory_data = get_itop_object(
                class_name="ServiceSubcategory",
                object_id=int(subcategory_id),
                output_fields=["name", "description"],
            )
            if subcategory_data:
                obj_data["servicesubcategory_details"] = subcategory_data
                subcategory_desc = subcategory_data.get("description", "")

        # AI Completeness check
        try:
            missing_info = await checker.check_completeness(
                title=obj_data.get("title", ""),
                description=obj_data.get("description", ""),
                service_desc=service_desc,
                subcategory_desc=subcategory_desc,
            )

            if missing_info:
                logger.info(f"{prefix}AI found missing info for {object_label}: {missing_info}")
                # Update iTop object with a log entry
                itop_client.update_object(
                    class_name=payload.class_name,
                    key=payload.id,
                    fields={"public_log": missing_info},
                    comment="AI assistant check: missing information",
                )
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
    """
    Handle webhook from iTop when an object is created.
    """
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

        # Synchronous processing
        logger.info(f"{prefix}Processing webhook synchronously for {object_label}")
        obj_data = await process_webhook_logic(payload, processing_id)
        return {"status": "success", "processing_id": str(processing_id), "data": obj_data}

    except Exception as e:
        logger.error(f"{prefix}Error processing webhook: {str(e)}", exc_info=True)
        if isinstance(e, HTTPException):
            raise e
        raise HTTPException(status_code=500, detail=str(e))
