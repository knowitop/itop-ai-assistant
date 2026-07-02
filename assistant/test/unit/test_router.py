import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

from fastapi.testclient import TestClient

from config import get_settings
from main import app


class TestWebhook(unittest.TestCase):
    def setUp(self):
        self.client = self.enterContext(TestClient(app))

    @patch("webhook.handler._run_enrichment_graph", new_callable=AsyncMock)
    def test_created_event_accepted(self, mock_run):
        mock_run.return_value = None

        response = self.client.post(
            "/webhook",
            json={"id": "123", "class": "UserRequest", "event": "created"},
        )

        self.assertEqual(response.status_code, 202)
        data = response.json()
        self.assertEqual(data["status"], "accepted")
        UUID(data["processing_id"])

    @patch("webhook.handler._run_enrichment_graph", new_callable=AsyncMock)
    def test_user_commented_event_accepted(self, mock_run):
        mock_run.return_value = None

        response = self.client.post(
            "/webhook",
            json={"id": "123", "class": "UserRequest", "event": "user_commented"},
        )

        self.assertEqual(response.status_code, 202)
        data = response.json()
        self.assertEqual(data["status"], "accepted")
        UUID(data["processing_id"])

    def test_assigned_event_marks_done(self):
        mock_deps = MagicMock()
        mock_deps.settings.webhook_token = None
        mock_deps.state_manager.mark_done = AsyncMock()
        self.client.app.state.deps = mock_deps

        response = self.client.post(
            "/webhook",
            json={"id": "123", "class": "UserRequest", "event": "assigned"},
        )

        self.assertEqual(response.status_code, 202)
        data = response.json()
        self.assertEqual(data["status"], "accepted")

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

    @patch("webhook.handler._run_enrichment_graph", new_callable=AsyncMock)
    def test_incident_class_accepted(self, mock_run):
        mock_run.return_value = None

        response = self.client.post(
            "/webhook",
            json={"id": "456", "class": "Incident", "event": "created"},
        )

        self.assertEqual(response.status_code, 202)
        data = response.json()
        self.assertEqual(data["status"], "accepted")
        UUID(data["processing_id"])

    @patch("webhook.handler._run_enrichment_graph", new_callable=AsyncMock)
    def test_each_request_gets_unique_processing_id(self, mock_run):
        mock_run.return_value = None

        r1 = self.client.post(
            "/webhook",
            json={"id": "1", "class": "UserRequest", "event": "created"},
        )
        r2 = self.client.post(
            "/webhook",
            json={"id": "2", "class": "UserRequest", "event": "created"},
        )

        self.assertNotEqual(r1.json()["processing_id"], r2.json()["processing_id"])


class TestWebhookAuth(unittest.TestCase):
    PAYLOAD = {"id": "123", "class": "UserRequest", "event": "created"}

    def setUp(self):
        # Token must be in env BEFORE the client starts: lifespan builds deps from settings.
        self.enterContext(patch.dict(os.environ, {"WEBHOOK_TOKEN": "test-secret"}))
        get_settings.cache_clear()
        self.addCleanup(get_settings.cache_clear)
        self.client = self.enterContext(TestClient(app))

    def test_missing_token_rejected(self):
        response = self.client.post("/webhook", json=self.PAYLOAD)
        self.assertEqual(response.status_code, 401)

    def test_wrong_token_rejected(self):
        response = self.client.post("/webhook", json=self.PAYLOAD, headers={"X-Auth-Token": "wrong"})
        self.assertEqual(response.status_code, 401)

    @patch("webhook.handler._run_enrichment_graph", new_callable=AsyncMock)
    def test_correct_token_accepted(self, mock_run):
        response = self.client.post("/webhook", json=self.PAYLOAD, headers={"X-Auth-Token": "test-secret"})
        self.assertEqual(response.status_code, 202)


class TestWebhookNoAuthConfigured(unittest.TestCase):
    def test_no_token_configured_accepts_unauthenticated(self):
        os.environ.pop("WEBHOOK_TOKEN", None)
        get_settings.cache_clear()
        self.addCleanup(get_settings.cache_clear)

        with (
            TestClient(app) as client,
            patch("webhook.handler._run_enrichment_graph", new_callable=AsyncMock),
        ):
            response = client.post("/webhook", json={"id": "123", "class": "UserRequest", "event": "created"})
        self.assertEqual(response.status_code, 202)


if __name__ == "__main__":
    unittest.main()
