"""The outer frame of a run: opened once, at the entry point that accepted the
trigger.

The shell (`shell.py`) deliberately does not own this — a run is opened exactly
once, and only the entry point knows what to do with a failure: a webhook logs
it and returns (its caller is iTop, already answered 202), a request lets it
propagate so FastAPI answers 500. What the two must not differ in is the record
they leave, which is why the frame lives here instead of in each entry point.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from itop_ai_assistant.journal import TriggerKind
from itop_ai_assistant.pipelines.context import RunContext

if TYPE_CHECKING:
    from itop_ai_assistant.deps import AppDeps

logger = logging.getLogger(__name__)


@asynccontextmanager
async def journalled_run(
    deps: "AppDeps",
    run: RunContext,
    *,
    kind: TriggerKind,
    subject: str,
    event: str,
) -> AsyncIterator[None]:
    """Record one run from start to finish, whatever trigger opened it.

    Journal writes are non-fatal by contract; the exception itself is always
    re-raised — swallowing it is the entry point's decision, not the frame's.
    """
    processing_id = run.processing_id
    await deps.journal.start(processing_id, subject=subject, event=event, module=run.module, kind=kind)
    try:
        yield
    except Exception as e:
        await deps.journal.finish(processing_id, "failed", error=f"{type(e).__name__}: {e}")
        raise
    else:
        await deps.journal.finish(processing_id, "done")
