import logging
import os

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from itop.client import ITopClient

# Load environment variables
load_dotenv()

# Logging configuration
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="iTop Webhook Handler")

# Initialize iTop Client from environment variables
ITOP_URL = os.getenv("ITOP_URL", "http://localhost/webservices/rest.php")
ITOP_USER = os.getenv("ITOP_USER")
ITOP_PWD = os.getenv("ITOP_PWD")
ITOP_TOKEN = os.getenv("ITOP_TOKEN")

# App configuration
APP_HOST = os.getenv("APP_HOST", "0.0.0.0")
APP_PORT = int(os.getenv("APP_PORT", "8000"))

itop_client = ITopClient(url=ITOP_URL, auth_user=ITOP_USER, auth_pwd=ITOP_PWD, auth_token=ITOP_TOKEN)


class WebhookPayload(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: int = Field(..., description="ID of the object in iTop")
    class_name: str = Field(..., alias="class", description="Class name of the object in iTop")


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


@app.post("/webhook")
async def handle_webhook(payload: WebhookPayload):
    """
    Handle webhook from iTop when an object is created.
    """
    logger.debug(f"Received webhook: {payload}")
    try:
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
            service_id = obj_data.get("service_id")
            if service_id:
                service_data = get_itop_object(
                    class_name="Service", object_id=int(service_id), output_fields=["name", "description"]
                )
                if service_data:
                    obj_data["service_details"] = service_data

            subcategory_id = obj_data.get("servicesubcategory_id")
            if subcategory_id:
                subcategory_data = get_itop_object(
                    class_name="ServiceSubcategory",
                    object_id=int(subcategory_id),
                    output_fields=["name", "description"],
                )
                if subcategory_data:
                    obj_data["servicesubcategory_details"] = subcategory_data

        logger.info(f"Successfully processed webhook for {payload.class_name}::{payload.id}")
        return {"status": "success", "data": obj_data}

    except Exception as e:
        logger.error(f"Error processing webhook: {str(e)}", exc_info=True)
        if isinstance(e, HTTPException):
            raise e
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=APP_HOST, port=APP_PORT)
