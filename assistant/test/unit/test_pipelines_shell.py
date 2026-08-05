"""The run shell's contract, on a module that is not intake.

`test_intake_pipeline.py` covers the same phases through the real module; this
file exists so the contract is pinned by a second, minimal implementation —
otherwise "generic" is only a claim about code that has one caller.
"""

import unittest
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from itop_ai_assistant.domain.ticket import Ticket
from itop_ai_assistant.pipelines.context import RunContext
from itop_ai_assistant.pipelines.models import ObjectRef
from itop_ai_assistant.pipelines.shell import TicketRun
from itop_ai_assistant.webhook.models import WebhookPayload


def _payload() -> WebhookPayload:
    return WebhookPayload.model_validate({"id": "123", "class": "Change", "event": "created"})


def _run() -> RunContext:
    return RunContext(processing_id=uuid4(), module="probe")


class _ProbeRun(TicketRun):
    """Records the order of the phases and what each one was handed."""

    def __init__(self, payload, run, deps):
        super().__init__(payload, run, deps)
        self.phases: list[str] = []
        self.stop_with: str | None = None
        self.body_raises: Exception | None = None
        self.seen: tuple[Ticket, str] | None = None
        self.bundle_in_body = None

    async def stop_reason(self, ticket: Ticket, ai_name: str) -> str | None:
        self.phases.append("guard")
        return self.stop_with

    async def body(self, ticket: Ticket, ai_name: str) -> None:
        self.phases.append("body")
        self.seen = (ticket, ai_name)
        # The shell must have resolved the iTop bundle before handing over
        self.bundle_in_body = self.bundle
        if self.body_raises:
            raise self.body_raises


class ShellTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.ticket = Ticket(obj_class="Change", id="123", status="new")

        self.deps = MagicMock()
        self.deps.journal = AsyncMock()
        self.deps.state_manager.acquire_lock = AsyncMock(return_value=True)
        self.deps.state_manager.release_lock = AsyncMock()

        self.bundle = MagicMock()
        self.bundle.ticket_repo.fetch = AsyncMock(return_value=self.ticket)
        self.deps.itop.ai_person_name = AsyncMock(return_value="ai-assistant")
        self.deps.itop.for_principal = AsyncMock(return_value=self.bundle)

        self.run = _ProbeRun(_payload(), _run(), self.deps)

    def journalled(self) -> list[str]:
        return [call.args[1] for call in self.deps.journal.add_step.await_args_list]


class TestPhaseOrder(ShellTestCase):
    async def test_guard_runs_before_the_body_and_both_get_the_ticket(self):
        outcome = await self.run.execute()

        self.assertEqual(outcome.status, "done")
        self.assertEqual(self.run.phases, ["guard", "body"])
        self.assertEqual(self.run.seen, (self.ticket, "ai-assistant"))
        self.assertIs(self.run.bundle_in_body, self.bundle)
        self.assertEqual(self.journalled(), [], "a clean run leaves the trace to the module")

    async def test_itop_is_reached_as_the_runs_principal(self):
        await self.run.execute()

        self.deps.itop.for_principal.assert_awaited_once()
        principal = self.deps.itop.for_principal.await_args.args[0]
        comment = self.deps.itop.for_principal.await_args.kwargs["comment"]
        self.assertIs(principal, self.run.run.principal)
        self.assertIn("probe", comment)
        self.assertIn(str(self.run.processing_id), comment)
        self.deps.itop.get.assert_not_called()

    async def test_label_is_the_lock_key(self):
        await self.run.execute()

        self.assertEqual(self.run.label, "Change::123")
        self.deps.state_manager.acquire_lock.assert_awaited_once_with("Change::123")
        self.deps.state_manager.release_lock.assert_awaited_once_with("Change::123")


class TestSkips(ShellTestCase):
    """Every stop reports itself the same way twice: a journal step and the outcome."""

    async def test_lock_held_stops_before_any_iTop_call(self):
        self.deps.state_manager.acquire_lock.return_value = False

        outcome = await self.run.execute()

        self.assertEqual(outcome.status, "skipped")
        self.assertEqual(self.run.phases, [])
        self.deps.itop.get.assert_not_called()
        self.assertEqual(self.journalled(), ["lock"])
        self.deps.state_manager.release_lock.assert_not_called()

    async def test_missing_object_stops_before_the_guard(self):
        self.bundle.ticket_repo.fetch.return_value = None

        outcome = await self.run.execute()

        self.assertEqual(outcome.status, "skipped")
        self.assertEqual(self.run.phases, [])
        self.assertEqual(self.journalled(), ["fetch"])
        self.deps.state_manager.release_lock.assert_awaited_once()

    async def test_guard_reason_stops_the_body_and_is_journalled_verbatim(self):
        self.run.stop_with = "not our business"

        outcome = await self.run.execute()

        self.assertEqual(self.run.phases, ["guard"])
        self.assertEqual(self.journalled(), ["guard"])
        detail = self.deps.journal.add_step.await_args.args[2]
        self.assertEqual(detail, "not our business")
        self.assertEqual((outcome.status, outcome.detail), ("skipped", "not our business"))


class TestFailure(ShellTestCase):
    async def test_body_error_propagates_and_still_releases_the_lock(self):
        """The entry point turns this into a failed run — the shell must not swallow it."""
        self.run.body_raises = RuntimeError("boom")

        with self.assertRaises(RuntimeError):
            await self.run.execute()

        self.deps.state_manager.release_lock.assert_awaited_once_with("Change::123")


class TestHandleClassmethod(ShellTestCase):
    async def test_handle_builds_one_instance_per_call(self):
        """State lives on the instance, so the registry must never hold a singleton."""
        seen: list[int] = []

        class Counting(_ProbeRun):
            async def body(self, ticket, ai_name):
                seen.append(id(self))

        await Counting.handle(_payload(), _run(), self.deps)
        await Counting.handle(_payload(), _run(), self.deps)

        self.assertEqual(len(seen), 2)
        self.assertNotEqual(seen[0], seen[1])

    async def test_handle_takes_a_bare_object_ref(self):
        """The shell knows nothing about triggers — a plain reference is enough."""
        outcome = await _ProbeRun.handle(ObjectRef(obj_class="Change", id="123"), _run(), self.deps)

        self.assertEqual(outcome.status, "done")
