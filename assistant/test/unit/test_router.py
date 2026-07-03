import unittest
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

from fastapi.testclient import TestClient

from config import ItopConfig, LlmConfig, SecurityConfig, TicketMappingConfig
from main import app


def _mock_deps(security: SecurityConfig | None = None, configured: bool = True) -> MagicMock:
    """AppDeps double: sections served from memory, iTop/journal stubbed out."""
    sections = {
        "security": security or SecurityConfig(),
        "itop": ItopConfig(url="http://itop/rest.php", token="tok") if configured else ItopConfig(),
        "llm": LlmConfig(base_url="http://llm/v1", model="test-model") if configured else LlmConfig(),
        "ticket_mapping": TicketMappingConfig(),
    }

    deps = MagicMock()
    deps.config_store.get = AsyncMock(side_effect=lambda module, model: sections[module])
    deps.journal = AsyncMock()
    deps.state_manager.acquire_lock = AsyncMock(return_value=True)
    deps.state_manager.release_lock = AsyncMock()
    deps.state_manager.mark_done = AsyncMock()
    bundle = MagicMock()
    bundle.ticket_repo.fetch = AsyncMock(return_value=None)  # "not found" → graph is skipped
    deps.itop.get = AsyncMock(return_value=bundle)
    return deps


class WebhookTestCase(unittest.TestCase):
    def setUp(self):
        self.client = self.enterContext(TestClient(app))
        self.deps = _mock_deps()
        self.client.app.state.deps = self.deps


class TestWebhook(WebhookTestCase):
    def test_created_event_accepted(self):
        response = self.client.post(
            "/webhook",
            json={"id": "123", "class": "UserRequest", "event": "created"},
        )

        self.assertEqual(response.status_code, 202)
        data = response.json()
        self.assertEqual(data["status"], "accepted")
        UUID(data["processing_id"])

    def test_user_commented_event_accepted(self):
        response = self.client.post(
            "/webhook",
            json={"id": "123", "class": "UserRequest", "event": "user_commented"},
        )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["status"], "accepted")

    def test_assigned_event_marks_done(self):
        response = self.client.post(
            "/webhook",
            json={"id": "123", "class": "UserRequest", "event": "assigned"},
        )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["status"], "accepted")

    def test_unsupported_class_rejected(self):
        response = self.client.post(
            "/webhook",
            json={"id": "123", "class": "Change", "event": "created"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("Unsupported class", response.json()["detail"])

    def test_invalid_payload_rejected(self):
        response = self.client.post("/webhook", json={"wrong": "payload"})
        self.assertEqual(response.status_code, 422)

    def test_invalid_event_rejected(self):
        response = self.client.post(
            "/webhook",
            json={"id": "123", "class": "UserRequest", "event": "unknown_event"},
        )
        self.assertEqual(response.status_code, 422)

    def test_incident_class_accepted(self):
        response = self.client.post(
            "/webhook",
            json={"id": "456", "class": "Incident", "event": "created"},
        )

        self.assertEqual(response.status_code, 202)
        UUID(response.json()["processing_id"])

    def test_each_request_gets_unique_processing_id(self):
        r1 = self.client.post("/webhook", json={"id": "1", "class": "UserRequest", "event": "created"})
        r2 = self.client.post("/webhook", json={"id": "2", "class": "UserRequest", "event": "created"})

        self.assertNotEqual(r1.json()["processing_id"], r2.json()["processing_id"])


class TestWebhookAuth(unittest.TestCase):
    PAYLOAD = {"id": "123", "class": "UserRequest", "event": "created"}

    def setUp(self):
        self.client = self.enterContext(TestClient(app))
        self.client.app.state.deps = _mock_deps(security=SecurityConfig(webhook_token="test-secret"))

    def test_missing_token_rejected(self):
        response = self.client.post("/webhook", json=self.PAYLOAD)
        self.assertEqual(response.status_code, 401)

    def test_wrong_token_rejected(self):
        response = self.client.post("/webhook", json=self.PAYLOAD, headers={"X-Auth-Token": "wrong"})
        self.assertEqual(response.status_code, 401)

    def test_correct_token_accepted(self):
        response = self.client.post("/webhook", json=self.PAYLOAD, headers={"X-Auth-Token": "test-secret"})
        self.assertEqual(response.status_code, 202)


class TestWebhookNoAuthConfigured(WebhookTestCase):
    def test_no_token_configured_accepts_unauthenticated(self):
        response = self.client.post("/webhook", json={"id": "123", "class": "UserRequest", "event": "created"})
        self.assertEqual(response.status_code, 202)


class TestWebhookNotConfigured(unittest.TestCase):
    def setUp(self):
        self.client = self.enterContext(TestClient(app))
        self.client.app.state.deps = _mock_deps(configured=False)

    def test_webhook_disabled_until_setup_complete(self):
        response = self.client.post("/webhook", json={"id": "123", "class": "UserRequest", "event": "created"})

        self.assertEqual(response.status_code, 503)
        self.assertIn("not configured", response.json()["detail"])
        self.assertIn("/api/setup/status", response.json()["detail"])


if __name__ == "__main__":
    unittest.main()
