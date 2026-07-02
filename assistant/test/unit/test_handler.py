import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from domain.ticket import Ticket
from webhook.handler import process_webhook_logic
from webhook.models import WebhookPayload


def _payload(event: str = "created") -> WebhookPayload:
    return WebhookPayload.model_validate({"id": "123", "class": "UserRequest", "event": event})


class TestProcessWebhookLock(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.deps = MagicMock()
        self.state_manager = self.deps.state_manager
        self.state_manager.acquire_lock = AsyncMock(return_value=True)
        self.state_manager.release_lock = AsyncMock()
        self.state_manager.mark_done = AsyncMock()

        self.fetch = AsyncMock(return_value=Ticket(obj_class="UserRequest", id="123"))
        self.deps.ticket_repo.fetch = self.fetch

        run_patch = patch("webhook.handler._run_enrichment_graph", new_callable=AsyncMock)
        self.mock_run = run_patch.start()
        self.addCleanup(run_patch.stop)

    async def test_lock_not_acquired_skips_processing(self):
        self.state_manager.acquire_lock.return_value = False

        await process_webhook_logic(_payload(), uuid4(), self.deps)

        self.fetch.assert_not_called()
        self.mock_run.assert_not_called()
        self.state_manager.release_lock.assert_not_called()

    async def test_lock_acquired_runs_graph_and_releases(self):
        await process_webhook_logic(_payload(), uuid4(), self.deps)

        self.fetch.assert_awaited_once_with("UserRequest", "123")
        self.mock_run.assert_awaited_once()
        self.state_manager.release_lock.assert_awaited_once_with("UserRequest::123")

    async def test_lock_released_on_graph_failure(self):
        self.mock_run.side_effect = RuntimeError("LLM down")

        with self.assertRaises(RuntimeError):
            await process_webhook_logic(_payload(), uuid4(), self.deps)

        self.state_manager.release_lock.assert_awaited_once_with("UserRequest::123")

    async def test_ticket_not_found_skips_graph_and_releases(self):
        self.fetch.return_value = None

        await process_webhook_logic(_payload(), uuid4(), self.deps)

        self.mock_run.assert_not_called()
        self.state_manager.release_lock.assert_awaited_once_with("UserRequest::123")

    async def test_assigned_event_marks_done_without_lock(self):
        await process_webhook_logic(_payload("assigned"), uuid4(), self.deps)

        self.state_manager.mark_done.assert_awaited_once_with("UserRequest::123")
        self.state_manager.acquire_lock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
