"""The one file that names the tracing libraries.

Imported from a single line inside the enabled branch of `tracing.setup_tracing`,
so with tracing off none of this is loaded at all. Reached no other way.

Neither the receiver nor its vendor is named here: the exporter speaks OTLP and
the spans carry OpenInference attributes, both of which are conventions rather
than a product. Pointing `TRACING_ENDPOINT` at a different OTLP receiver is the
whole of a migration.
"""

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from uuid import UUID

from openinference.instrumentation import using_attributes
from openinference.instrumentation.langchain import LangChainInstrumentor
from openinference.semconv.resource import ResourceAttributes
from openinference.semconv.trace import OpenInferenceSpanKindValues, SpanAttributes
from opentelemetry import trace as trace_api
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Tracer
from opentelemetry.util.types import Attributes

from itop_ai_assistant.config import Settings
from itop_ai_assistant.pipelines.ports import RunTracer
from itop_ai_assistant.state.journal import TriggerKind

logger = logging.getLogger(__name__)


class OtelRunTracer:
    """Opens one span per run, and lets the instrumentation fill in the rest."""

    def __init__(self, tracer: Tracer):
        self._tracer = tracer

    @contextmanager
    def run_span(
        self,
        processing_id: UUID | str,
        subject: str,
        event: str,
        module: str,
        kind: TriggerKind,
        principal: str,
        dry_run: bool,
    ) -> Iterator[None]:
        """The run's root span, carrying exactly what the journal records.

        `session.id` is the run's subject — the same `Class::Id` that keys the
        processing lock and the ticket state, so every run over one ticket lands
        in one session without a second identity being invented for it.

        `using_attributes` puts the session and the metadata on the *child*
        spans too, the ones the LangChain instrumentation makes. Without it,
        "every model call of this run" would be a walk down the tree instead of
        a filter.
        """
        metadata = {
            "processing_id": str(processing_id),
            "module": module,
            "kind": kind,
            "principal": principal,
            "dry_run": dry_run,
        }
        # Typed, not inferred: a mixed str/bool dict infers as dict[str, object],
        # which the span API rejects. Only visible to mypy where the packages
        # are installed — the pre-commit gate does not have them.
        attributes: Attributes = {
            SpanAttributes.OPENINFERENCE_SPAN_KIND: OpenInferenceSpanKindValues.CHAIN.value,
            SpanAttributes.SESSION_ID: subject,
            "itop_ai.processing_id": str(processing_id),
            "itop_ai.subject": subject,
            "itop_ai.event": event,
            "itop_ai.module": module,
            "itop_ai.kind": kind,
            "itop_ai.principal": principal,
            "itop_ai.dry_run": dry_run,
        }
        with (
            using_attributes(session_id=subject, metadata=metadata),
            self._tracer.start_as_current_span(f"{module}.{event}", attributes=attributes),
        ):
            yield


def install(settings: Settings) -> RunTracer:
    """Make this process trace: a global provider plus LangChain instrumentation.

    Global, and instrumenting the framework rather than `create_llm`, is what
    covers all three consumers of the model at once — the intake agent, the
    selfcheck pipeline and the wizard's model probe — without any of them
    learning that tracing exists. LangGraph needs no instrumentor of its own:
    it runs on the same LangChain callbacks.

    `BatchSpanProcessor`, not the simple one: the simple processor exports
    inline, which would put the receiver's availability into the run's hot
    path. The SDK flushes the buffer at exit through its own `atexit` hook, so
    no port here grows a `shutdown()`.
    """
    resource = Resource({ResourceAttributes.PROJECT_NAME: settings.tracing_project_name})
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=settings.tracing_endpoint)))
    trace_api.set_tracer_provider(provider)
    LangChainInstrumentor().instrument(tracer_provider=provider)
    logger.info(f"LLM tracing enabled: project {settings.tracing_project_name!r}, endpoint {settings.tracing_endpoint}")
    return OtelRunTracer(provider.get_tracer(__name__))
