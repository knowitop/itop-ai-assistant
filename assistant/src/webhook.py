import logging
import os

from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from agent import ITopInfoChecker
from itop.client import ITopClient

# Load environment variables
load_dotenv()

# Logging configuration
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="iTop Webhook Handler")

# Initialize iTop Client and Agent
ITOP_URL = os.getenv("ITOP_URL", "http://localhost/webservices/rest.php")
ITOP_USER = os.getenv("ITOP_USER")
ITOP_PWD = os.getenv("ITOP_PWD")
ITOP_TOKEN = os.getenv("ITOP_TOKEN")

itop_client = ITopClient(url=ITOP_URL, auth_user=ITOP_USER, auth_pwd=ITOP_PWD, auth_token=ITOP_TOKEN)

LLM_MODEL_NAME = os.getenv("LLM_MODEL", "gpt-3.5-turbo")
checker = ITopInfoChecker(LLM_MODEL_NAME)

# App configuration
APP_HOST = os.getenv("APP_HOST", "0.0.0.0")
APP_PORT = int(os.getenv("APP_PORT", "8000"))


class WebhookPayload(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: int = Field(..., description="ID of the object in iTop")
    class_name: str = Field(..., alias="class", description="Class name of the object in iTop")
    is_async: bool = Field(True, alias="async", description="Whether to process the request asynchronously")


def get_itop_object(class_name: str, object_id: int, output_fields: list[str]) -> dict:
    """
    Fetch a single object from iTop and return its fields.
    """
    logger.debug(f"Fetching object details for {class_name}::{object_id}")
    result = itop_client.get_objects(class_name=class_name, key=object_id, output_fields=output_fields)
    logger.debug(f"iTop API response for {class_name}::{object_id}: {result}")

    objects = result.get("objects")
    if not objects:
        logger.warning(f"Object {class_name}::{object_id} not found in iTop")
        return {}

    obj_key = list(objects.keys())[0]
    return objects[obj_key]["fields"]


async def process_webhook_logic(payload: WebhookPayload) -> dict:
    """
    Core logic for processing a webhook.
    """
    # Fetch detailed information about the main object
    obj_data = get_itop_object(
        class_name=payload.class_name,
        object_id=payload.id,
        output_fields=["ref", "title", "description", "service_id", "servicesubcategory_id"],
    )

    if not obj_data:
        raise HTTPException(status_code=404, detail=f"Object {payload.class_name}::{payload.id} not found in iTop")

    # If it's a UserRequest or Incident, try to fetch Service and ServiceSubcategory details
    if payload.class_name in ["UserRequest", "Incident"]:
        service_desc = ""
        subcategory_desc = ""

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
                logger.info(f"AI found missing info for {payload.class_name}::{payload.id}: {missing_info}")
                # Update iTop object with a log entry
                itop_client.update_object(
                    class_name=payload.class_name,
                    key=payload.id,
                    fields={"public_log": missing_info},
                    comment="AI assistant check: missing information",
                )
                obj_data["ai_check_result"] = missing_info
            else:
                logger.info(f"AI check passed for {payload.class_name}::{payload.id}")
                obj_data["ai_check_result"] = "OK"
        except Exception as ai_err:
            logger.error(f"AI completeness check failed for {payload.class_name}::{payload.id}: {ai_err}")
            obj_data["ai_check_result"] = "Error"

    logger.info(f"Successfully processed webhook for {payload.class_name}::{payload.id}")
    return obj_data


@app.post("/webhook")
async def handle_webhook(payload: WebhookPayload, background_tasks: BackgroundTasks):
    """
    Handle webhook from iTop when an object is created.
    """
    logger.debug(f"Received webhook: {payload}")
    try:
        if payload.is_async:
            logger.info(f"Processing webhook asynchronously for {payload.class_name}::{payload.id}")
            background_tasks.add_task(process_webhook_logic, payload)
            return {"status": "accepted", "message": "Webhook processing started in background"}

        # Synchronous processing
        obj_data = await process_webhook_logic(payload)
        return {"status": "success", "data": obj_data}

    except Exception as e:
        logger.error(f"Error processing webhook: {str(e)}", exc_info=True)
        if isinstance(e, HTTPException):
            raise e
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=APP_HOST, port=APP_PORT)
