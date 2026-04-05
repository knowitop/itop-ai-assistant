import unittest
from unittest.mock import AsyncMock, patch
from uuid import UUID

from fastapi.testclient import TestClient

from main import app


class TestWebhook(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

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

    @patch("webhook.handler.state_manager")
    def test_assigned_event_marks_done(self, mock_state_manager):
        mock_state_manager.mark_done = AsyncMock()

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


if __name__ == "__main__":
    unittest.main()
