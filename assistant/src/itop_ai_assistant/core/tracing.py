"""Whether this process traces LLM calls, and what a run gets when it does not.

Nothing here names a tracing library. That is what makes the promise of
ADR-029 checkable rather than aspirational: with tracing off, the packages are
not imported, no exporter exists and nothing leaves the process. The single
import of `tracing_otel` below, inside the enabled branch, is the whole seam.

Installed once per process, at startup (`main.lifespan`), because OpenTelemetry
instrumentation is global — which is also why the switch is an env-only
`Settings` field and not a runtime section anyone could flip from the admin UI.
"""

import logging
from contextlib import AbstractContextManager, nullcontext
from uuid import UUID

from itop_ai_assistant.config import Settings
from itop_ai_assistant.pipelines.ports import RunTracer
from itop_ai_assistant.state.journal import TriggerKind

logger = logging.getLogger(__name__)


class NullRunTracer:
    """The tracer of a process that does not trace — the default, not a stand-in.

    Every run still opens its frame; the frame just records nothing beyond the
    journal. Costs one `nullcontext()` per run and holds no state.
    """

    def run_span(
        self,
        processing_id: UUID | str,
        subject: str,
        event: str,
        module: str,
        kind: TriggerKind,
        principal: str,
        dry_run: bool,
    ) -> AbstractContextManager[None]:
        return nullcontext()


def setup_tracing(settings: Settings) -> RunTracer:
    """Install the instrumentation for this process, if it is asked for.

    A missing package is reported and survived rather than fatal: no feature of
    this service needs tracing to work, and refusing to boot over observability
    would trade the whole service for a diagnostic. The same reasoning as
    `main.check_module_prompts` applies to a broken prompt override — loud, but
    not at the cost of the service.
    """
    if not settings.tracing_enabled:
        return NullRunTracer()
    try:
        from itop_ai_assistant.core import tracing_otel
    except ImportError as e:
        logger.error(
            f"Tracing is enabled but its packages are missing ({e}) — running without it. "
            f"Install them with `uv sync --extra tracing`"
        )
        return NullRunTracer()
    return tracing_otel.install(settings)
