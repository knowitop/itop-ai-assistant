"""What the run core needs from the outside — and nothing more.

Each protocol here is cut to one consumer's actual usage, not to what the
implementation happens to offer: `TicketRun` locks and releases, so `LockPort`
has two methods, even though the real `TicketStateManager` also reads and
increments state and closes its pool. That is the whole point — a run cannot reach what its type
does not mention.

Two rules hold this together:

* **Members are declared as methods or read-only properties, never as plain
  attributes.** A protocol attribute is invariant: `state_manager: LockPort`
  would reject an `AppDeps` whose field is typed `TicketStateManager`, since
  invariance demands the exact type. A read-only `@property` is covariant and
  accepts it. This is checked by strict mypy, which is the gate in pre-commit.
* **Implementations do not inherit these protocols.** `AppDeps` satisfies
  `RunDeps` structurally and knows nothing about this module; making it inherit
  would drag the Redis/Qdrant/langchain imports of `core/deps.py` back into the
  core, which is exactly what this file exists to prevent.

`aclose()` appears in no port on purpose: the connection pool belongs to the
composition root, and no run should be able to close it.
"""

from typing import Protocol, TypeVar, runtime_checkable
from uuid import UUID

from langchain_core.language_models.chat_models import BaseChatModel
from pydantic import BaseModel

from itop_ai_assistant.config import LlmConfig
from itop_ai_assistant.itop.connection import AiIdentity
from itop_ai_assistant.repositories.sets import ItopRepositories
from itop_ai_assistant.settings.config_store import ConfigStore
from itop_ai_assistant.settings.prompt_store import PromptStore
from itop_ai_assistant.state.journal import RunStatus, TriggerKind
from itop_ai_assistant.vector import SimilarSearch

TState = TypeVar("TState", bound=BaseModel)


@runtime_checkable
class LockPort(Protocol):
    """The per-object lock, as the run shell uses it: take it, give it back."""

    async def acquire_lock(self, ticket_ref: str) -> bool: ...

    async def release_lock(self, ticket_ref: str) -> None: ...


class ObjectStatePort(LockPort, Protocol):
    """The lock plus generic per-object state, namespaced by the caller's own module name.

    Wider than `LockPort` because a module legitimately needs more than the
    shell does — but still narrower than the manager, which also owns its
    Redis pool. Deliberately as ignorant of field names as `ConfigStore.get`
    (`settings/config_store.py`) is of a module's config fields: a module
    passes its own model and reads it back, so this port never has to change
    when a module's state shape does, and two modules keyed under different
    names never collide (TASK-047).

    Not `runtime_checkable`: nothing holds this port itself as a field pydantic
    must `isinstance`-validate — a module wraps it in its own concrete adapter
    (`agents/intake/state.py::IntakeState`) before handing it to langgraph's
    `context_schema`, and a concrete class needs no such decorator.
    """

    async def get(self, module: str, ticket_ref: str, model: type[TState]) -> TState: ...

    async def increment(self, module: str, ticket_ref: str, field: str) -> None: ...

    async def set_flag(self, module: str, ticket_ref: str, field: str) -> None: ...


class StepJournal(Protocol):
    """Appending steps to a run's trace — all the shell and the agent loop do.

    Journal writes are non-fatal by contract; that is a property of the
    implementation, and this port does not let a caller depend on the opposite.
    """

    async def add_step(self, processing_id: UUID | str, node: str, detail: str = "") -> None: ...


class RunFrameJournal(StepJournal, Protocol):
    """Opening and closing a run. Only `journalled_run` needs this half.

    Kept apart from `StepJournal` so that the shell and the agent loop, which
    record steps inside a frame someone else opened, cannot open or finish a
    run by accident — a run is opened exactly once, at the entry point.
    """

    async def start(
        self,
        processing_id: UUID | str,
        subject: str,
        event: str,
        module: str,
        kind: TriggerKind = "webhook",
        principal: str = "service",
    ) -> None: ...

    async def finish(self, processing_id: UUID | str, status: RunStatus, error: str | None = None) -> None: ...


class RunDeps(Protocol):
    """What a module's handler is handed — the boundary, not the core.

    The trigger registry needs one type for every handler, and modules differ in
    what they use, so this one is a composite. It is not `AppDeps` under another
    name: no `settings` (runtime config is read through `config_store`, which is
    what makes an admin edit apply without a restart) and no `aclose()`.

    A handler takes this apart into the narrow ports above and passes those into
    the core. That hand-off is the boundary: everything below it names only what
    it uses.
    """

    @property
    def config_store(self) -> ConfigStore: ...

    @property
    def prompt_store(self) -> PromptStore: ...

    @property
    def journal(self) -> RunFrameJournal: ...

    @property
    def itop(self) -> ItopRepositories: ...

    @property
    def ai_identity(self) -> AiIdentity: ...

    @property
    def state_manager(self) -> ObjectStatePort: ...

    @property
    def vector_search(self) -> SimilarSearch: ...

    def create_llm(self, llm: LlmConfig, model: str | None = None) -> BaseChatModel: ...
