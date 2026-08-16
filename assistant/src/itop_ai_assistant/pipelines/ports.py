"""What the run core needs from the outside — and nothing more.

Each protocol here is cut to one consumer's actual usage, not to what the
implementation happens to offer: `TicketRun` locks and releases, so `LockPort`
has two methods, even though the real `TicketStateManager` also counts rounds
and closes its pool. That is the whole point — a run cannot reach what its type
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

from typing import Protocol, runtime_checkable
from uuid import UUID

from itop_ai_assistant.core.principal import Principal
from itop_ai_assistant.repositories.sets import RepositorySet
from itop_ai_assistant.settings.config_store import ConfigStore
from itop_ai_assistant.settings.prompt_store import PromptStore
from itop_ai_assistant.state.journal import RunStatus, TriggerKind
from itop_ai_assistant.state.ticket_state import TicketState
from itop_ai_assistant.vector import ChunkStore


@runtime_checkable
class LockPort(Protocol):
    """The per-object lock, as the run shell uses it: take it, give it back."""

    async def acquire_lock(self, ticket_ref: str) -> bool: ...

    async def release_lock(self, ticket_ref: str) -> None: ...


@runtime_checkable
class TicketStatePort(LockPort, Protocol):
    """The lock plus the per-ticket AI state a module reads and advances.

    Wider than `LockPort` because a module legitimately needs more than the
    shell does — `ai_done`, the round counters — but still narrower than the
    manager, which also owns its Redis pool.

    `runtime_checkable` is required, not decorative: `IntakeContext` holds this
    port and is handed to langgraph as the agent's `context_schema`, so pydantic
    builds a validator for it and validates the field with `isinstance` — which
    raises outright on a plain protocol. Note what this buys and what it does
    not: a runtime protocol check tests for the presence of the members, never
    their signatures, so it is weaker than the old concrete-class check. The
    real guarantee here is static (strict mypy), and this decorator only keeps
    the runtime from refusing to build.
    """

    async def get(self, ticket_ref: str) -> TicketState: ...

    async def mark_done(self, ticket_ref: str) -> None: ...

    async def increment_rounds(self, ticket_ref: str) -> None: ...

    async def increment_classify_rounds(self, ticket_ref: str) -> None: ...


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


class ItopAccess(Protocol):
    """Getting at iTop from inside a run.

    One method: every run acts through `for_principal`, naming its own
    `principal` and `comment` (`.claude/rules/core.md`) — even a run that
    never writes, like `selfcheck`. `ai_person_name` answers off the
    connection's own client whatever the run acts as, which is what keeps the
    intake loop guard honest.
    """

    async def for_principal(self, principal: Principal, *, comment: str) -> RepositorySet: ...

    async def ai_person_name(self) -> str: ...


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
    def itop(self) -> ItopAccess: ...

    @property
    def state_manager(self) -> TicketStatePort: ...

    @property
    def vector_store(self) -> ChunkStore: ...
