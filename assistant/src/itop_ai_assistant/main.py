import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from redis.exceptions import RedisError

from itop_ai_assistant.admin.router import router as admin_router
from itop_ai_assistant.config import ItopConfig, LlmConfig, SecurityConfig, get_settings, missing_setup
from itop_ai_assistant.core.background import build_background_tasks
from itop_ai_assistant.core.deps import build_deps
from itop_ai_assistant.core.tracing import setup_tracing
from itop_ai_assistant.pipelines.registry import ModuleInfo, build_registry
from itop_ai_assistant.settings.prompt_store import PromptOrigin, PromptStore
from itop_ai_assistant.settings.prompt_validation import PromptValidationError
from itop_ai_assistant.util.build_info import get_build_info
from itop_ai_assistant.webhook.router import router

settings = get_settings()

logging.basicConfig(
    level=settings.log_level.upper(),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)

logger = logging.getLogger(__name__)


build = get_build_info()


async def check_module_prompts(module: ModuleInfo, prompt_store: PromptStore) -> None:
    """Validate one module's prompt set at startup, by origin of the template.

    A broken template of ours is a defect of the distribution and stops the
    boot, as it always has. A broken *override* only warns: refusing to start
    would take away the admin UI the override was written in and is fixed in,
    leaving `redis-cli` inside the container as the only way out (REQ-005).
    The override stays in effect, so the module fails every run until someone
    fixes the text — visibly, and with the admin UI up.

    Errors an override cannot cause — a missing template, a name nobody
    registered — land on the first branch by themselves: an override only ever
    shadows a packaged prompt, it cannot add or remove one.
    """
    if module.validate_prompts is None:
        return
    prompts = await prompt_store.get(module.name)
    for name, reason in sorted(prompts.ignored.items()):
        logger.warning(f"Prompt override {module.name}/{name} is not applied: {reason}")
    try:
        module.validate_prompts(prompts.effective)
    except PromptValidationError as e:
        origins = prompts.origins
        if any(origins.get(name, PromptOrigin.DEFAULT) is PromptOrigin.DEFAULT for name in e.errors):
            raise
        for name, message in sorted(e.errors.items()):
            logger.warning(
                f"Prompt {module.name}/{name} is overridden ({origins[name]}) and broken: {message}. "
                f"The override stays in effect — fix it in the admin UI; until then "
                f"module {module.name!r} fails on every run"
            )


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    logger.info(f"iTop AI Assistant {build.version} ({build.commit or 'no commit'})")
    registry = build_registry(settings)
    # Before the dependencies, because the instrumentation is global and has to
    # be in place before anything it instruments is constructed (ADR-029).
    deps = build_deps(settings, registry, tracer=setup_tracing(settings))
    for module in registry.modules:
        await check_module_prompts(module, deps.prompt_store)

    # The installation's own id, generated once and written before anyone asks
    # for it: the setup wizard shows it on its welcome screen, before a single
    # setting has been saved. A start without Redis is not an error worth
    # failing over — both values are still written on first ask (REQ-009 R1).
    try:
        await deps.install.register()
    except RedisError as e:
        logger.warning(f"Install state unavailable, this installation is not on record yet: {e}")

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

    # Every periodic loop in the process: the vector sweep (infrastructure) and
    # one per registered schedule trigger. What each of them does is re-checked
    # per tick from the runtime config, so nothing here decides that.
    tasks = build_background_tasks(deps, registry)
    tasks.start()

    app.state.deps = deps
    # A separate attribute, not reached through `deps` — `vector/router.py`
    # is served off this one alone, so it needs nothing from `core/` (TASK-037).
    app.state.vector = deps.vector
    app.state.registry = registry
    app.state.tasks = tasks
    try:
        yield
    finally:
        # Loops hold the vector store client and the iTop client — stop them first
        await tasks.stop()
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
