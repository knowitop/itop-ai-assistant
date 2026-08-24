"""Admin API: configuration, prompts and processing-run monitoring.

Backend for the future admin UI. Protected by the `security.admin_token`
runtime setting (standard bearer auth: `Authorization: Bearer <token>`),
separate from the webhook token.
"""

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ValidationError
from redis.exceptions import RedisError

from itop_ai_assistant.admin.setup import router as setup_router
from itop_ai_assistant.config import TelemetryConfig
from itop_ai_assistant.core.api_deps import (
    get_config_store,
    get_document_builder,
    get_journal,
    get_prompt_store,
    remember_admin_language,
    verify_admin_token,
)
from itop_ai_assistant.pipelines.registry import ModuleInfo
from itop_ai_assistant.request.router import router as request_router
from itop_ai_assistant.settings.config_store import ConfigStore
from itop_ai_assistant.settings.module_locales import load_translation
from itop_ai_assistant.settings.prompt_store import PromptStore, PromptStoreError
from itop_ai_assistant.settings.prompt_validation import PromptValidationError
from itop_ai_assistant.state.journal import ProcessingRun, RunJournal
from itop_ai_assistant.telemetry.builder import DocumentBuilder
from itop_ai_assistant.telemetry.document import TelemetryDocument
from itop_ai_assistant.util.build_info import is_release_build
from itop_ai_assistant.vector import router as vector_router

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", dependencies=[Depends(verify_admin_token), Depends(remember_admin_language)])
router.include_router(setup_router)
router.include_router(vector_router)
router.include_router(request_router)


def _module_or_404(request: Request, module: str) -> ModuleInfo:
    info = request.app.state.registry.get_module(module)
    if info is None:
        raise HTTPException(status_code=404, detail=f"Unknown module: {module}")
    return info


@router.get("/modules")
async def list_modules(request: Request, lang: str | None = None) -> list[dict]:
    """Every module and its triggers, in `lang` where the module has it.

    `lang` is the language the admin UI is showing, not the browser's: the two
    differ as soon as an administrator picks one. Anything the module has no
    translation for stays in the English of its own declaration (ADR-030).
    """
    registry = request.app.state.registry
    modules = []
    for m in registry.modules:
        texts = load_translation(m.locales_dir, lang)
        modules.append(
            {
                "name": m.name,
                "description": texts.module_description(m.description),
                "has_config": m.config_model is not None,
                "prompts": list(m.prompt_names),
                # What the module can be asked to do synchronously; the UI builds
                # its form from the schema instead of knowing the module
                "requests": [
                    {
                        "action": route.action,
                        "summary": texts.action_summary(route.action, route.summary),
                        "input_schema": texts.action_schema(route.action, route.input_model.model_json_schema()),
                    }
                    for route in registry.requests_for(m.name)
                ],
                # What the clock starts on its own. Read-only: the period is a
                # config field of the module, edited on the same screen.
                "schedules": [
                    {
                        "name": route.name,
                        "summary": texts.schedule_summary(route.name, route.summary),
                        "default_interval": route.default_interval,
                    }
                    for route in registry.schedules_for(m.name)
                ],
            }
        )
    return modules


@router.get("/config/{module}")
async def get_config(
    module: str, request: Request, config_store: Annotated[ConfigStore, Depends(get_config_store)]
) -> dict:
    info = _module_or_404(request, module)
    if info.config_model is None:
        raise HTTPException(status_code=404, detail=f"Module {module!r} has no config")
    cfg = await config_store.get(module, info.config_model)
    return cfg.model_dump()


@router.get("/config/{module}/schema")
async def get_config_schema(module: str, request: Request, lang: str | None = None) -> dict:
    """The form's whole knowledge of this module: fields, hints and their texts.

    Labels, descriptions and section headings come out translated into `lang`
    where the module ships that language — the UI renders what it is given and
    keeps no field names of its own (ADR-025, ADR-030).
    """
    info = _module_or_404(request, module)
    if info.config_model is None:
        raise HTTPException(status_code=404, detail=f"Module {module!r} has no config")
    texts = load_translation(info.locales_dir, lang)
    return texts.config_schema(info.config_model.model_json_schema())


def _field_errors(error: ValidationError) -> list[dict[str, str]]:
    """Flatten a ValidationError into `{field, message}` per failure.

    The admin form puts each message next to the input it belongs to, which
    `str(ValidationError)` — one blob with pydantic's own URLs in it — cannot
    be split into. A model validator has an empty `loc` and stays a
    form-level message: the failure is a relation between fields, and picking
    one of them to blame would be a guess.
    """
    return [{"field": ".".join(str(part) for part in err["loc"]), "message": err["msg"]} for err in error.errors()]


@router.put("/config/{module}")
async def update_config(
    module: str, body: dict[str, Any], request: Request, config_store: Annotated[ConfigStore, Depends(get_config_store)]
) -> dict:
    info = _module_or_404(request, module)
    if info.config_model is None:
        raise HTTPException(status_code=404, detail=f"Module {module!r} has no config")
    try:
        cfg = await config_store.set(module, body, info.config_model)
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=_field_errors(e)) from e
    logger.info(f"Config for module {module!r} updated via admin API")
    return cfg.model_dump()


@router.delete("/config/{module}", status_code=204)
async def reset_config(
    module: str, request: Request, config_store: Annotated[ConfigStore, Depends(get_config_store)]
) -> None:
    _module_or_404(request, module)
    await config_store.reset(module)
    logger.info(f"Config for module {module!r} reset to defaults via admin API")


@router.get("/prompts/{module}")
async def get_prompts(
    module: str, request: Request, prompt_store: Annotated[PromptStore, Depends(get_prompt_store)]
) -> dict:
    info = _module_or_404(request, module)
    prompts = await prompt_store.get(module)
    # Recomputed on every read, never cached from startup: an override applies
    # without a restart, so a verdict from boot time would go stale both ways —
    # missing a prompt broken since, and still accusing one already fixed.
    broken: dict[str, str] = {}
    if info.validate_prompts is not None:
        try:
            info.validate_prompts(prompts.effective)
        except PromptValidationError as e:
            broken = e.errors
    return {
        "prompts": prompts.effective,
        "overridden": sorted(prompts.runtime),
        "broken": broken,
        "ignored": prompts.ignored,
    }


class PromptUpdate(BaseModel):
    text: str


@router.put("/prompts/{module}/{name}")
async def update_prompt(
    module: str,
    name: str,
    body: PromptUpdate,
    request: Request,
    prompt_store: Annotated[PromptStore, Depends(get_prompt_store)],
) -> dict:
    info = _module_or_404(request, module)

    prompts = (await prompt_store.get(module)).effective
    if name not in prompts:
        raise HTTPException(status_code=404, detail=f"Unknown prompt {name!r}. Known: {sorted(prompts)}")

    if info.validate_prompts is not None:
        try:
            info.validate_prompts({**prompts, name: body.text})
        except PromptValidationError as e:
            # Only what is wrong with *this* template blocks the save. Another
            # broken prompt in the same module must not make this one
            # unfixable — that is the dead end of REQ-005 moved inside the UI.
            if name in e.errors:
                raise HTTPException(status_code=422, detail=f"{name}: {e.errors[name]}") from e
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e)) from e

    try:
        await prompt_store.set(module, name, body.text)
    except PromptStoreError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    logger.info(f"Prompt {module}/{name} updated via admin API")
    return {"name": name, "text": body.text}


@router.delete("/prompts/{module}/{name}", status_code=204)
async def reset_prompt(
    module: str, name: str, request: Request, prompt_store: Annotated[PromptStore, Depends(get_prompt_store)]
) -> None:
    _module_or_404(request, module)
    await prompt_store.reset(module, name)
    logger.info(f"Prompt {module}/{name} reset via admin API")


@router.get("/runs")
async def list_runs(
    journal: Annotated[RunJournal, Depends(get_journal)],
    limit: int = 50,
    subject: str | None = None,
    status: str | None = None,
) -> list[ProcessingRun]:
    return await journal.list(limit=limit, subject=subject, status=status)


@router.get("/runs/{processing_id}")
async def get_run(processing_id: str, journal: Annotated[RunJournal, Depends(get_journal)]) -> ProcessingRun:
    run = await journal.get(processing_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Run {processing_id} not found")
    return run


@router.get("/telemetry")
async def telemetry_state(
    request: Request,
    config_store: Annotated[ConfigStore, Depends(get_config_store)],
) -> dict:
    """Whether the daily document is on, and whether it would actually leave.

    Two answers rather than three: the screen needs the outcome, not the terms
    it is made of. `sending` is false with the switch on whenever this is not
    a build we published (`util/build_info.py::is_release_build`) — the case
    that would otherwise read as "telemetry is on but my installation never
    shows up".

    The installation id is deliberately not here: it belongs to the
    installation rather than to its telemetry, and travels in
    `GET /api/setup/status` with the rest of what every screen already asks
    for (REQ-009 R1).
    """
    settings = request.app.state.deps.settings
    enabled = (await config_store.get("telemetry", TelemetryConfig)).enabled
    return {
        "enabled": enabled,
        "sending": enabled and (is_release_build() or settings.telemetry_test_mode),
    }


@router.get("/telemetry/preview")
async def telemetry_preview(
    builder: Annotated[DocumentBuilder, Depends(get_document_builder)],
) -> TelemetryDocument:
    """The exact document that would leave today (REQ-009 R7).

    Not a diagnostic endpoint and not a description of the format: it calls
    the same builder the sender calls, so it cannot drift from what is sent
    the way a documented example always eventually does. This is what answers
    a customer's security team asking what leaves their network, without
    making them read our source.

    Answers with the switch off as well, which is the whole point — "show me
    what would go out if I turned this on" is the question asked *before*
    turning it on. Nothing here opens a connection to the receiver: the client
    is built inside `TelemetryDeckSink.send`, which this path never reaches.
    """
    try:
        return await builder.build()
    except RedisError as e:
        # The builder does not swallow this and must not (REQ-009 R5): without
        # Redis there are no counters and no id, so there is no document. But
        # a stack trace is no answer to "what leaves today" — the honest one
        # is that the installation cannot describe itself right now, which is
        # also exactly what the sender does at this moment.
        logger.info(f"Telemetry preview unavailable, installation state cannot be read: {e}")
        raise HTTPException(status_code=503, detail="Installation state is unavailable — nothing to describe") from e
