"""The run shell: what every module's run goes through, whatever it does inside.

lock → fetch → guard → body → release. A module supplies two things — why a run
must stop (`stop_reason`) and what it actually does (`body`); the rest is the
same for all of them, so a new module inherits the platform's invariants instead
of remembering to re-implement them.

The trigger is not part of that contract: the shell is handed an `ObjectRef`, so
the same run serves a webhook event and a synchronous request. What differs is
only what happens to the `RunOutcome` afterwards — dropped by the webhook,
returned to the caller by the request.

The outer frame is deliberately *not* here: `journal.start` / `finish` and the
top-level exception capture belong to whoever accepted the trigger
(`pipelines/runner.py::journalled_run`, called from the entry points). A run is
opened once, at the entry point. Exceptions from the body propagate out of
`execute()` for exactly that reason.
"""

import logging
from abc import ABC, abstractmethod
from uuid import UUID

from itop_ai_assistant.domain.ticket import Ticket
from itop_ai_assistant.itop.connection import AiIdentity
from itop_ai_assistant.pipelines.context import RunContext
from itop_ai_assistant.pipelines.models import ObjectRef, RunOutcome
from itop_ai_assistant.pipelines.ports import LockPort, RunDeps, StepJournal
from itop_ai_assistant.repositories.sets import ItopRepositories, RepositorySet

logger = logging.getLogger(__name__)


class TicketRun(ABC):
    """One run over one iTop object, under a per-object lock.

    Instantiated **per run**, never per registration: the attributes below are
    this run's state, and a single shared instance in the registry would race
    between concurrent webhooks for the same object.

    Takes the four ports it actually uses, not the container: the shell locks,
    reads iTop, knows the AI's own name and records steps, and its type says
    so. `handle()` is where the
    container handed to a module gets taken apart into them.
    """

    # Assigned by execute() before the guard and the body run — there is nothing
    # to read before that.
    repos: RepositorySet

    def __init__(
        self,
        ref: ObjectRef,
        run: RunContext,
        deps: RunDeps,
        *,
        lock: LockPort,
        itop: ItopRepositories,
        ai_identity: AiIdentity,
        journal: StepJournal,
    ) -> None:
        self.ref = ref
        self.run = run
        # Kept for the module's own body: what a concrete run needs beyond the
        # shell's three ports is the module's business, not the shell's.
        self.deps = deps
        self.lock = lock
        self.itop = itop
        self.ai_identity = ai_identity
        self.journal = journal
        self.label = ref.label

    @property
    def processing_id(self) -> UUID:
        return self.run.processing_id

    @classmethod
    async def handle(cls, ref: ObjectRef, run: RunContext, deps: RunDeps) -> RunOutcome:
        """What a module registers as its route — fits both handler types.

        A webhook route drops the outcome; a request route returns it to the
        caller. That is the whole difference between the two triggers here.

        This is also the boundary: the registry has one handler type for every
        module, so the container arrives here, and here is where it becomes the
        narrow ports the shell is constructed with.
        """
        return await cls(
            ref,
            run,
            deps,
            lock=deps.state_manager,
            itop=deps.itop,
            ai_identity=deps.ai_identity,
            journal=deps.journal,
        ).execute()

    async def execute(self) -> RunOutcome:
        if not await self.lock.acquire_lock(self.label):
            logger.info(f"[{self.processing_id}] {self.label} is already being processed, skipping")
            return await self.skip("lock", "ticket is already being processed")
        try:
            self.repos = await self.itop.for_principal(self.run.principal, comment=self.run.comment)
            ticket = await self.repos.ticket_repo.fetch(self.ref.obj_class, self.ref.id)
            if ticket is None:
                logger.warning(f"[{self.processing_id}] {self.label} not found in iTop, skipping")
                return await self.skip("fetch", "ticket not found in iTop")
            ai_name = await self.ai_identity.ai_person_name()
            # The guard runs before the body, not as middleware: it needs no LLM
            # and it saves the catalog round-trip to iTop on a no-op webhook
            reason = await self.stop_reason(ticket, ai_name)
            if reason:
                logger.info(f"[{self.processing_id}] {self.label}: {reason}")
                return await self.skip("guard", reason)
            await self.body(ticket, ai_name)
            return RunOutcome(status="done")
        finally:
            await self.lock.release_lock(self.label)

    @abstractmethod
    async def stop_reason(self, ticket: Ticket, ai_name: str) -> str | None:
        """Why this object must not be processed, or None to proceed."""

    @abstractmethod
    async def body(self, ticket: Ticket, ai_name: str) -> None:
        """What the module does once the guard let it through."""

    async def step(self, node: str, detail: str = "") -> None:
        """One journal step for this run. Journal writes are non-fatal by contract."""
        await self.journal.add_step(self.processing_id, node, detail)

    async def skip(self, node: str, reason: str) -> RunOutcome:
        """Record why the run stopped and report it — the same text both ways."""
        await self.step(node, reason)
        return RunOutcome(status="skipped", detail=reason)
