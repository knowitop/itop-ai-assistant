import logging
import os

from dotenv import load_dotenv
from fastapi import FastAPI

from router import router

# Load environment variables
load_dotenv()

# Logging configuration
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")

# FastAPI application initialization
app = FastAPI(title="iTop AI Assistant")
app.include_router(router)

if __name__ == "__main__":
    import uvicorn

    APP_HOST = os.getenv("APP_HOST", "0.0.0.0")
    APP_PORT = int(os.getenv("APP_PORT", "8000"))

    logger = logging.getLogger(__name__)
    logger.info(f"Starting iTop AI Assistant on {APP_HOST}:{APP_PORT}")
    uvicorn.run(app, host=APP_HOST, port=APP_PORT)
