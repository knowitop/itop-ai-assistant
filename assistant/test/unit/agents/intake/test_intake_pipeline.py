import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from itop_ai_assistant.agents.intake.config import IntakeConfig
from itop_ai_assistant.agents.intake.prompts import MODULE
from itop_ai_assistant.agents.intake.run import IntakeRun, handle_assigned
from itop_ai_assistant.agents.intake.state import TicketState
from itop_ai_assistant.domain.ticket import LogEntry, Ticket
from itop_ai_assistant.pipelines.context import RunContext
from itop_ai_assistant.pipelines.models import ObjectRef
from itop_ai_assistant.webhook.models import WebhookPayload


def _run() -> RunContext:
    return RunContext(processing_id=uuid4(), module="intake")


def _payload(event: str = "created") -> WebhookPayload:
    return WebhookPayload.model_validate({"id": "123", "class": "Incident", "event": event})


def _ticket(status: str = "new", public_log: list[LogEntry] | None = None) -> Ticket:
    return Ticket(obj_class="Incident", id="123", status=status, public_log=public_log or [])


class TestHandleTicketEvent(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.deps = MagicMock()
        self.state_manager = self.deps.state_manager
        self.state_manager.acquire_lock = AsyncMock(return_value=True)
        self.state_manager.release_lock = AsyncMock()
        self.state_manager.set_flag = AsyncMock()
        self.state_manager.get = AsyncMock(return_value=TicketState())

        self.repos = MagicMock()
        self.fetch = AsyncMock(return_value=_ticket())
        self.repos.ticket_repo.fetch = self.fetch
        self.deps.itop.for_principal = AsyncMock(return_value=self.repos)
        self.deps.ai_identity.ai_person_name = AsyncMock(return_value="ai-assistant")
        self.deps.journal = AsyncMock()
        self.deps.config_store.get = AsyncMock(return_value=IntakeConfig())

        run_patch = patch.object(IntakeRun, "body", new_callable=AsyncMock)
        self.mock_run = run_patch.start()
        self.addCleanup(run_patch.stop)

    def _journalled_steps(self) -> list[str]:
        return [call.args[1] for call in self.deps.journal.add_step.await_args_list]

    async def test_lock_not_acquired_skips_processing(self):
        self.state_manager.acquire_lock.return_value = False

        await IntakeRun.handle(_payload(), _run(), self.deps)

        self.fetch.assert_not_called()
        self.mock_run.assert_not_called()
        self.state_manager.release_lock.assert_not_called()
        self.assertEqual(self._journalled_steps(), ["lock"])

    async def test_lock_acquired_runs_agent_and_releases(self):
        await IntakeRun.handle(_payload(), _run(), self.deps)

        self.fetch.assert_awaited_once_with("Incident", "123")
        self.mock_run.assert_awaited_once()
        self.state_manager.release_lock.assert_awaited_once_with("Incident::123")

    async def test_lock_released_on_agent_failure(self):
        self.mock_run.side_effect = RuntimeError("LLM down")

        with self.assertRaises(RuntimeError):
            await IntakeRun.handle(_payload(), _run(), self.deps)

        self.state_manager.release_lock.assert_awaited_once_with("Incident::123")

    async def test_ticket_not_found_skips_agent_and_releases(self):
        self.fetch.return_value = None

        await IntakeRun.handle(_payload(), _run(), self.deps)

        self.mock_run.assert_not_called()
        self.state_manager.release_lock.assert_awaited_once_with("Incident::123")
        self.assertEqual(self._journalled_steps(), ["fetch"])

    async def test_assigned_event_marks_done_without_lock(self):
        await handle_assigned(_payload("assigned"), _run(), self.deps)

        self.state_manager.set_flag.assert_awaited_once_with(MODULE, "Incident::123", "ai_done")
        self.state_manager.acquire_lock.assert_not_called()

    async def test_a_bare_object_ref_runs_the_same_way(self):
        """What the request trigger hands over: no event, same run, an outcome back."""
        outcome = await IntakeRun.handle(ObjectRef(obj_class="Incident", id="123"), _run(), self.deps)

        self.assertEqual(outcome.status, "done")
        self.mock_run.assert_awaited_once()

    async def test_a_finished_ticket_reports_why_it_was_skipped(self):
        self.state_manager.get.return_value = TicketState(ai_done=True)

        outcome = await IntakeRun.handle(ObjectRef(obj_class="Incident", id="123"), _run(), self.deps)

        self.assertEqual((outcome.status, outcome.detail), ("skipped", "already processed (ai_done)"))
        self.mock_run.assert_not_called()


class TestGuard(unittest.IsolatedAsyncioTestCase):
    """The three guard checks live in the pipeline, before the agent starts."""

    def setUp(self):
        self.deps = MagicMock()
        self.deps.state_manager.acquire_lock = AsyncMock(return_value=True)
        self.deps.state_manager.release_lock = AsyncMock()
        self.deps.state_manager.get = AsyncMock(return_value=TicketState())
        self.deps.journal = AsyncMock()

        self.repos = MagicMock()
        self.repos.ticket_repo.fetch = AsyncMock(return_value=_ticket())
        self.deps.itop.for_principal = AsyncMock(return_value=self.repos)
        self.deps.ai_identity.ai_person_name = AsyncMock(return_value="ai-assistant")
        self.deps.config_store.get = AsyncMock(return_value=IntakeConfig())

        run_patch = patch.object(IntakeRun, "body", new_callable=AsyncMock)
        self.mock_run = run_patch.start()
        self.addCleanup(run_patch.stop)

    async def _run(self) -> None:
        await IntakeRun.handle(_payload(), _run(), self.deps)

    def _assert_guarded(self) -> None:
        self.mock_run.assert_not_called()
        nodes = [call.args[1] for call in self.deps.journal.add_step.await_args_list]
        self.assertEqual(nodes, ["guard"])

    async def test_ai_done_stops(self):
        self.deps.state_manager.get.return_value = TicketState(ai_done=True)

        await self._run()

        self._assert_guarded()

    async def test_inactive_status_stops(self):
        self.repos.ticket_repo.fetch.return_value = _ticket(status="assigned")

        await self._run()

        self._assert_guarded()

    async def test_last_public_entry_is_ours_stops(self):
        self.repos.ticket_repo.fetch.return_value = _ticket(
            public_log=[
                LogEntry(user_login="John Doe", message="printer is dead"),
                LogEntry(user_login="ai-assistant", message="which printer?"),
            ]
        )

        await self._run()

        self._assert_guarded()

    async def test_last_public_entry_is_users_proceeds(self):
        self.repos.ticket_repo.fetch.return_value = _ticket(
            public_log=[
                LogEntry(user_login="ai-assistant", message="which printer?"),
                LogEntry(user_login="John Doe", message="the one in room 3"),
            ]
        )

        await self._run()

        self.mock_run.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
