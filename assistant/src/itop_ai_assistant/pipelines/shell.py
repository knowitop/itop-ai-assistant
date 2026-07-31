"""The run shell: what every module's run goes through, whatever it does inside.

lock → fetch → guard → body → release. A module supplies two things — why a run
must stop (`stop_reason`) and what it actually does (`body`); the rest is the
same for all of them, so a new module inherits the platform's invariants instead
of remembering to re-implement them.

The outer frame is deliberately *not* here: `journal.start` / `finish` and the
top-level exception capture belong to whoever accepted the trigger
(`webhook/router.py`). A run is opened once, at the entry point. Exceptions from
the body propagate out of `execute()` for exactly that reason.
"""

import logging
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING
from uuid import UUID

if TYPE_CHECKING:
    from itop_ai_assistant.deps import AppDeps, ItopBundle
    from itop_ai_assistant.domain.ticket import Ticket
    from itop_ai_assistant.webhook.models import WebhookPayload

logger = logging.getLogger(__name__)


class TicketRun(ABC):
    """One run over one iTop object, under a per-object lock.

    Instantiated **per run**, never per registration: the attributes below are
    this run's state, and a single shared instance in the registry would race
    between concurrent webhooks for the same object.
    """

    # Assigned by execute() before the guard and the body run — there is nothing
    # to read before that.
    bundle: "ItopBundle"

    def __init__(self, payload: "WebhookPayload", processing_id: UUID, deps: "AppDeps") -> None:
        self.payload = payload
        self.processing_id = processing_id
        self.deps = deps
        self.label = f"{payload.obj_class}::{payload.id}"

    @classmethod
    async def handle(cls, payload: "WebhookPayload", processing_id: UUID, deps: "AppDeps") -> None:
        """What a module registers as its route — matches `PipelineHandler`."""
        await cls(payload, processing_id, deps).execute()

    async def execute(self) -> None:
        if not await self.deps.state_manager.acquire_lock(self.label):
            logger.info(f"[{self.processing_id}] {self.label} is already being processed, skipping")
            await self.step("lock", "ticket is already being processed — skipped")
            return
        try:
            self.bundle = await self.deps.itop.get()
            ticket = await self.bundle.ticket_repo.fetch(self.payload.obj_class, self.payload.id)
            if ticket is None:
                logger.warning(f"[{self.processing_id}] {self.label} not found in iTop, skipping")
                await self.step("fetch", "ticket not found in iTop — skipped")
                return
            ai_name = await self.bundle.ticket_repo.get_ai_person_name()
            # The guard runs before the body, not as middleware: it needs no LLM
            # and it saves the catalog round-trip to iTop on a no-op webhook
            reason = await self.stop_reason(ticket, ai_name)
            if reason:
                logger.info(f"[{self.processing_id}] {self.label}: {reason}")
                await self.step("guard", reason)
                return
            await self.body(ticket, ai_name)
        finally:
            await self.deps.state_manager.release_lock(self.label)

    @abstractmethod
    async def stop_reason(self, ticket: "Ticket", ai_name: str) -> str | None:
        """Why this object must not be processed, or None to proceed."""

    @abstractmethod
    async def body(self, ticket: "Ticket", ai_name: str) -> None:
        """What the module does once the guard let it through."""

    async def step(self, node: str, detail: str = "") -> None:
        """One journal step for this run. Journal writes are non-fatal by contract."""
        await self.deps.journal.add_step(self.processing_id, node, detail)
