from dataclasses import dataclass
from typing import Any

import redis.asyncio as aioredis
from fastapi import Request
from langchain.chat_models import init_chat_model
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models.chat_models import BaseChatModel

from itop_ai_assistant.config import LlmConfig, Settings
from itop_ai_assistant.core.llm_counters import LlmCallCounter
from itop_ai_assistant.core.llm_providers import get_provider
from itop_ai_assistant.core.tracing import NullRunTracer
from itop_ai_assistant.itop.connection import AiIdentity, ItopConnection
from itop_ai_assistant.itop.write_policy import WritePolicy
from itop_ai_assistant.pipelines.ports import RunTracer
from itop_ai_assistant.pipelines.registry import TriggerRegistry
from itop_ai_assistant.repositories.sets import ItopRepositories
from itop_ai_assistant.settings.config_store import ConfigStore, RedisConfigStore
from itop_ai_assistant.settings.prompt_store import FilePromptStore, PromptStore, RedisPromptStore
from itop_ai_assistant.state.counters import DailyCounters
from itop_ai_assistant.state.install import InstallIdentity
from itop_ai_assistant.state.journal import RunJournal
from itop_ai_assistant.state.ticket_state import TicketStateManager
from itop_ai_assistant.telemetry.builder import DocumentBuilder
from itop_ai_assistant.util.redis_keyspace import days_to_seconds
from itop_ai_assistant.vector import SimilarSearch, VectorSubsystem
from itop_ai_assistant.vector import build as build_vector  # aliased: this module has its own `build_deps`


@dataclass
class AppDeps:
    """Application-wide dependencies, assembled once at startup (composition root).

    Handed to entry points, and no deeper: the run core takes the narrow ports
    of `pipelines/ports.py` instead, and a module's handler is where this
    container is taken apart into them. `AppDeps` satisfies `RunDeps`
    structurally and deliberately does not inherit it — knowing about the ports
    would put the infrastructure imports above back into the core.

    `aclose()` is on purpose absent from every port: ownership of the connection
    pool stays here, so no run can be typed into closing it.

    `vector` is one field, not five (TASK-037): the composition root calls
    `vector.build()` once, the same way it calls `build_registry(settings)` for
    modules, and holds whatever came back — it does not know, and does not
    need to know, that the subsystem happens to be made of a store, two Redis
    journals and a search door internally. `vector_search` stays a property
    rather than a second stored field: `RunDeps.vector_search`
    (`pipelines/ports.py`) is unchanged by this task, and structural typing
    needs the member to exist on `AppDeps` itself, not one level down.
    `ai_identity` is the same trick again (TASK-038): `RunDeps.ai_identity`
    needs the member on `AppDeps` itself, and the value is just
    `itop_connection` under its narrower name — no second field for the same
    object. `create_llm` delegates to the free function below rather than
    wrapping a field: the function stays the one construction site
    `agents.md` documents, this method is only how a module reaches it
    through the port instead of importing the composition root directly.
    """

    settings: Settings
    itop: ItopRepositories
    # The connection is held next to the factory built over it, not inside it:
    # closing the pool is the composition root's business, and the factory is
    # kept free of a lifecycle method for the same reason no port has one.
    itop_connection: ItopConnection
    # Held here as well as inside `itop`, because the entry points need the
    # answer for a different reason: they stamp the run journal with it
    # (`RunContext.dry_run`), while the repository factory enforces it.
    write_policy: WritePolicy
    state_manager: TicketStateManager
    config_store: ConfigStore
    prompt_store: PromptStore
    journal: RunJournal
    # The day's activity, and the only aggregate that outlives the journal's
    # TTL and its capped index (`state/counters.py`). Filled from three levels
    # at once — the run frame, the iTop write layer, the model factory — and
    # read once a day by the telemetry document.
    counters: DailyCounters
    # What this installation remembers about itself between restarts: the id
    # it generated for itself, the admin-UI language last seen, and the dates
    # the daily send checks before it may send. A field of its own rather than
    # something reached through the builder, because the admin router writes
    # to it on every request carrying `?lang=` and the setup API when the
    # wizard finishes — different questions from assembling a document.
    install: InstallIdentity
    # The day's document, assembled on demand (REQ-009) — by the daily send
    # (`telemetry/sender.py`) and by the preview that shows an administrator
    # what will leave.
    telemetry: DocumentBuilder
    # The frame's second recorder, next to the journal it mirrors
    # (`pipelines/runner.py`). Deliberately absent from `RunDeps`: a module
    # neither reaches tracing nor knows it exists — the instrumentation is
    # global and the frame is opened above the module (ADR-029).
    tracer: RunTracer
    vector: VectorSubsystem

    @property
    def vector_search(self) -> SimilarSearch:
        return self.vector.vector_search

    @property
    def ai_identity(self) -> AiIdentity:
        return self.itop_connection

    def create_llm(self, llm: LlmConfig, model: str | None = None) -> BaseChatModel:
        """A model that counts what it does. The handler is attached here and
        not inside the factory, so the one caller who must *not* be counted —
        the wizard's endpoint probe — keeps calling the plain function
        (`core/llm_counters.py`)."""
        return create_llm(llm, model, callbacks=[LlmCallCounter(self.counters)])

    async def aclose(self) -> None:
        await self.itop_connection.aclose()
        await self.state_manager.aclose()
        await self.vector.aclose()


def get_deps(request: Request) -> "AppDeps":
    """The container, off the request — for `webhook`/`request`/`admin`, which
    hand it whole into the run core (`.claude/rules/core.md`: `AppDeps` goes to
    entry points and no deeper, and a module's `handle` is where it is taken
    apart into ports). A function that needs one field, not the container,
    belongs in `core/api_deps.py` instead. `vector/router.py` does not use
    either — it reaches its own subsystem through `request.app.state.vector`.
    """
    return request.app.state.deps


def create_llm(
    llm: LlmConfig, model: str | None = None, *, callbacks: list[BaseCallbackHandler] | None = None
) -> BaseChatModel:
    """Create an LLM client for the configured provider.

    `model` overrides the default `llm.model`; `llm.params` is forwarded
    verbatim to the provider's client (temperature, max_tokens, …).
    `callbacks` ride on the model and fire for every call it makes, the agent
    loop's included — `llm.params` may not contain them (`LlmConfig`).
    """
    provider = get_provider(llm.provider)
    kwargs: dict[str, Any] = dict(llm.params)
    if provider.base_url_mode != "unused" and llm.base_url:
        kwargs["base_url"] = llm.base_url
    if provider.api_key_mode == "required":
        kwargs["api_key"] = llm.api_key
    elif provider.api_key_mode == "optional":
        # Local endpoints (LM Studio) accept any key; the client requires one
        kwargs["api_key"] = llm.api_key or "unused"
    if callbacks:
        kwargs["callbacks"] = callbacks
    return init_chat_model(model or llm.model or "", model_provider=provider.langchain_provider, **kwargs)


def build_deps(settings: Settings, registry: TriggerRegistry, *, tracer: RunTracer | None = None) -> AppDeps:
    """Assemble the application-wide dependencies.

    `tracer` comes from `setup_tracing` in the lifespan, which is the only
    place instrumentation may be installed. Omitting it means "this process
    opens no runs" — the reindex CLI, and tests — not "tracing is optional
    here": a process that does open runs and passes nothing would journal them
    and trace nothing, silently.
    """
    # One shared Redis connection pool for state, journal, config, prompts and vector
    redis = aioredis.from_url(settings.redis_url, decode_responses=True)
    config_store = RedisConfigStore(redis, settings)
    state_manager = TicketStateManager(redis, ttl_seconds=days_to_seconds(settings.state_ttl_days))
    itop_connection = ItopConnection(config_store)
    write_policy = WritePolicy(config_store)
    counters = DailyCounters(redis)
    itop = ItopRepositories(itop_connection, config_store, write_policy, counters)
    install = InstallIdentity(redis)
    vector = build_vector(settings, redis, config_store, itop, counters)
    prompts_dirs = {m.name: m.prompts_dir for m in registry.modules if m.prompts_dir is not None}

    return AppDeps(
        settings=settings,
        itop=itop,
        itop_connection=itop_connection,
        write_policy=write_policy,
        state_manager=state_manager,
        config_store=config_store,
        prompt_store=RedisPromptStore(FilePromptStore(prompts_dirs, settings.prompts_dir), redis),
        journal=RunJournal(redis, ttl_seconds=days_to_seconds(settings.run_ttl_days)),
        counters=counters,
        install=install,
        telemetry=DocumentBuilder(settings, config_store, registry, counters, install, vector.vector_search),
        tracer=tracer or NullRunTracer(),
        vector=vector,
    )
