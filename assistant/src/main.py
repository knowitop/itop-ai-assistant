import logging

from fastapi import FastAPI

from config import get_settings
from webhook.router import router

settings = get_settings()

logging.basicConfig(
    level=settings.log_level.upper(),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)

app = FastAPI(title="iTop AI Assistant")
app.include_router(router)

if __name__ == "__main__":
    import uvicorn

    logger = logging.getLogger(__name__)
    logger.info(f"Starting iTop AI Assistant on {settings.app_host}:{settings.app_port}")
    uvicorn.run(app, host=settings.app_host, port=settings.app_port)
