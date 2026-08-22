"""The synchronous trigger: one call into a module, one answer to the caller.

Same registry, same run shell and same journal as the webhook path — the only
differences are that the caller waits for the outcome and that a failure reaches
them as a 500 instead of being logged and dropped.

Mounted under the admin router, so it is protected by the admin token for now.
Identity is deliberately absent: the run acts as the module's service account
(§8.2 — delegated principals arrive with the engineer console).
"""

import logging
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import ValidationError

from itop_ai_assistant.core.api_deps import require_configured, resolve_principal
from itop_ai_assistant.core.deps import AppDeps, get_deps
from itop_ai_assistant.pipelines.context import RunContext
from itop_ai_assistant.pipelines.models import RunOutcome
from itop_ai_assistant.pipelines.registry import RequestRoute
from itop_ai_assistant.pipelines.runner import journalled_run

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/modules", dependencies=[Depends(require_configured)])


class RunRequestResponse(RunOutcome):
    processing_id: UUID


@router.post("/{module}/{action}")
async def run_request(
    module: str,
    action: str,
    body: dict,
    request: Request,
    deps: Annotated[AppDeps, Depends(get_deps)],
) -> RunRequestResponse:
    route: RequestRoute | None = request.app.state.registry.resolve_request(module, action)
    if route is None:
        raise HTTPException(status_code=404, detail=f"Unknown request action: {module}/{action}")
    try:
        payload = route.input_model.model_validate(body)
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e

    run = RunContext(
        processing_id=uuid4(),
        module=module,
        principal=await resolve_principal(request),
        dry_run=await deps.write_policy.dry_run(),
    )
    subject = route.subject_of(payload)
    logger.info(f"[{run.processing_id}] Running {module}/{action} for {subject}")
    async with journalled_run(deps.journal, deps.tracer, run, kind="request", subject=subject, event=action):
        outcome = await route.handler(payload, run, deps)
    return RunRequestResponse(processing_id=run.processing_id, **outcome.model_dump())
