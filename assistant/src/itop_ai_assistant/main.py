import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from itop_ai_assistant.admin.router import router as admin_router
from itop_ai_assistant.build_info import get_build_info
from itop_ai_assistant.config import ItopConfig, LlmConfig, SecurityConfig, get_settings, missing_setup
from itop_ai_assistant.deps import build_deps
from itop_ai_assistant.pipelines.registry import build_registry
from itop_ai_assistant.vector.db import run_migrations
from itop_ai_assistant.vector.indexer import VectorIndexer
from itop_ai_assistant.webhook.router import router

settings = get_settings()

logging.basicConfig(
    level=settings.log_level.upper(),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)

logger = logging.getLogger(__name__)


build = get_build_info()


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    logger.info(f"iTop AI Assistant {build.version} ({build.commit or 'no commit'})")
    deps = build_deps(settings)
    registry = build_registry(settings)
    # Fail fast on missing or broken prompt templates instead of on a live ticket
    for module in registry.modules:
        if module.validate_prompts:
            module.validate_prompts(await deps.prompt_store.get(module.name))

    # Vector store is optional: no DATABASE_URL = Redis-only deployment, and a
    # failed migration degrades to "vector unavailable", never a boot failure
    if settings.database_url:
        try:
            await asyncio.to_thread(run_migrations, settings.database_url)
        except Exception as e:
            logger.warning(f"Postgres migrations failed — vector store unavailable until fixed: {e}")

    # Setup diagnostics against the *effective* config (Redis overrides > env)
    security = await deps.config_store.get("security", SecurityConfig)
    if security.webhook_token is None:
        logger.warning("Webhook token is not set — /webhook accepts unauthenticated requests")
    if security.admin_token is None:
        logger.warning("Admin token is not set — /api accepts unauthenticated requests")
    missing = missing_setup(
        await deps.config_store.get("itop", ItopConfig),
        await deps.config_store.get("llm", LlmConfig),
    )
    if missing:
        logger.warning(
            f"Setup incomplete: {'; '.join(missing)} — "
            "/webhook is disabled until configured via the admin API (/api/setup)"
        )

    # Background sweep exists only alongside Postgres; whether it actually
    # indexes is re-checked every tick from the runtime config (vector.enabled)
    indexer: VectorIndexer | None = None
    if settings.database_url:
        indexer = VectorIndexer(deps)
        indexer.start()

    app.state.deps = deps
    app.state.registry = registry
    app.state.vector_indexer = indexer
    try:
        yield
    finally:
        if indexer is not None:
            await indexer.stop()
        await deps.aclose()


app = FastAPI(title="iTop AI Assistant", version=build.version, lifespan=lifespan)
app.include_router(router)
app.include_router(admin_router)


def _find_ui_dist() -> Path | None:
    # The SPA build is not part of the Python package, so an installed
    # deployment has to be told where it is — the image sets UI_DIST_DIR.
    # Without it, walk up from this file looking for a source checkout, where
    # ui/ is a sibling of assistant/ at the repo root.
    if settings.ui_dist_dir is not None:
        candidate = settings.ui_dist_dir
        return candidate if (candidate / "index.html").is_file() else None
    here = Path(__file__).resolve()
    for root in here.parents[1:4]:
        candidate = root / "ui" / "dist"
        if (candidate / "index.html").is_file():
            return candidate
    return None


_ui_dist = _find_ui_dist()
if _ui_dist is not None:
    app.mount("/ui", StaticFiles(directory=_ui_dist, html=True), name="ui")

    @app.get("/", include_in_schema=False)
    async def index() -> RedirectResponse:
        return RedirectResponse("/ui/")


@app.get("/health")
async def health(request: Request) -> dict:
    try:
        await request.app.state.deps.state_manager.ping()
        redis_ok = True
    except Exception:
        redis_ok = False
    return {"status": "ok" if redis_ok else "degraded", "redis": redis_ok}


# Public like /health, and for the same reason: the setup wizard runs before
# an admin token exists, and "which build is this?" is the first support question.
@app.get("/version")
async def version() -> dict:
    return {"version": build.version, "commit": build.commit, "built_at": build.built_at}


if __name__ == "__main__":
    import uvicorn

    logger.info(f"Starting iTop AI Assistant on {settings.app_host}:{settings.app_port}")
    uvicorn.run(app, host=settings.app_host, port=settings.app_port)
