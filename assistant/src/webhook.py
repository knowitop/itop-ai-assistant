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


@app.post("/webhook")
async def handle_webhook(payload: WebhookPayload):
    """
    Handle webhook from iTop when an object is created.
    """
    logger.debug(f"Received webhook: {payload}")
    try:
        # Fetch detailed information about the object
        logger.debug(f"Fetching object details for {payload.class_name}::{payload.id}")
        result = itop_client.get_objects(
            class_name=payload.class_name, key=payload.id, output_fields=["ref", "title", "description"]
        )
        logger.debug(f"iTop API response: {result}")

        # Extract object data
        objects = result.get("objects")
        if not objects:
            logger.warning(f"Object {payload.class_name}::{payload.id} not found in iTop")
            raise HTTPException(status_code=404, detail=f"Object {payload.class_name}::{payload.id} not found in iTop")

        # iTop returns objects in a dict with keys like "ClassName::ID"
        obj_key = list(objects.keys())[0]
        obj_data = objects[obj_key]["fields"]

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
