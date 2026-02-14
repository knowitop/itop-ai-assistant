import os
import sys
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

# Add src to sys.path to import webhook and itop
sys.path.append(os.path.join(os.getcwd(), "src"))

from webhook import app


class TestWebhook(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    @patch("webhook.itop_client.get_objects")
    def test_webhook_success(self, mock_get_objects):
        # Arrange
        mock_get_objects.return_value = {
            "code": 0,
            "message": "Success",
            "objects": {
                "UserRequest::123": {
                    "fields": {"ref": "R-000123", "title": "Help me", "description": "I need help with my PC"}
                }
            },
        }

        payload = {"id": 123, "class": "UserRequest"}

        # Act
        response = self.client.post("/webhook", json=payload)

        # Assert
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["status"], "success")
        self.assertEqual(data["data"]["ref"], "R-000123")
        self.assertEqual(data["data"]["title"], "Help me")

        mock_get_objects.assert_called_once_with(
            class_name="UserRequest", key=123, output_fields=["ref", "title", "description"]
        )

    @patch("webhook.itop_client.get_objects")
    def test_webhook_not_found(self, mock_get_objects):
        # Arrange
        mock_get_objects.return_value = {"code": 0, "message": "Success", "objects": None}

        payload = {"id": 999, "class": "UserRequest"}

        # Act
        response = self.client.post("/webhook", json=payload)

        # Assert
        self.assertEqual(response.status_code, 404)
        self.assertIn("not found", response.json()["detail"])

    def test_webhook_invalid_payload(self):
        # Act
        response = self.client.post("/webhook", json={"wrong": "payload"})

        # Assert
        self.assertEqual(response.status_code, 422)


if __name__ == "__main__":
    unittest.main()
