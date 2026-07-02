import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from config import get_settings
from deps import build_deps
from graph.enrichment.prompts import build_enrichment_prompts
from pipelines.registry import build_registry
from webhook.router import router

settings = get_settings()

logging.basicConfig(
    level=settings.log_level.upper(),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    if settings.webhook_token is None:
        logger.warning("WEBHOOK_TOKEN is not set — /webhook accepts unauthenticated requests")
    deps = build_deps(settings)
    # Fail fast on missing or broken prompt templates instead of on a live ticket
    build_enrichment_prompts(await deps.prompt_store.get("enrichment"))
    app.state.deps = deps
    app.state.registry = build_registry(settings)
    try:
        yield
    finally:
        await deps.aclose()


app = FastAPI(title="iTop AI Assistant", lifespan=lifespan)
app.include_router(router)

if __name__ == "__main__":
    import uvicorn

    logger.info(f"Starting iTop AI Assistant on {settings.app_host}:{settings.app_port}")
    uvicorn.run(app, host=settings.app_host, port=settings.app_port)
