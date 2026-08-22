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

from itop_ai_assistant.pipelines.context import RunContext
from itop_ai_assistant.pipelines.ports import RunFrameJournal, RunTracer
from itop_ai_assistant.state.journal import TriggerKind

logger = logging.getLogger(__name__)


@asynccontextmanager
async def journalled_run(
    journal: RunFrameJournal,
    tracer: RunTracer,
    run: RunContext,
    *,
    kind: TriggerKind,
    subject: str,
    event: str,
) -> AsyncIterator[None]:
    """Record one run from start to finish, whatever trigger opened it.

    Two recorders, one set of facts: the journal (Redis, days, what the Runs
    screen reads) and the trace (a span tree, what the model was actually sent
    and answered). Both are filled from the same expressions below, which is
    the only reason "find the trace of this run" is a lookup by
    `processing_id` and not a search by timestamp.

    The span wraps the journal write rather than the other way round: journal
    writes are non-fatal by contract, and a Redis outage during one belongs
    inside the trace of the run it happened in. The exception itself is always
    re-raised — swallowing it is the entry point's decision, not the frame's;
    the span records it on its way out.
    """
    processing_id = run.processing_id
    # The label, never the credentials: reading under an engineer's token
    # leaves almost no trace in iTop, so this is where "who asked" lives —
    # and it must not become where their token lives.
    principal = run.principal.label
    with tracer.run_span(
        processing_id,
        subject=subject,
        event=event,
        module=run.module,
        kind=kind,
        principal=principal,
        dry_run=run.dry_run,
    ):
        await journal.start(
            processing_id,
            subject=subject,
            event=event,
            module=run.module,
            kind=kind,
            principal=principal,
            dry_run=run.dry_run,
        )
        try:
            yield
        except Exception as e:
            await journal.finish(processing_id, "failed", error=f"{type(e).__name__}: {e}")
            raise
        else:
            await journal.finish(processing_id, "done")
