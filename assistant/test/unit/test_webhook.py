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
        def side_effect(class_name, key, output_fields):
            if class_name == "UserRequest":
                return {
                    "code": 0,
                    "message": "Success",
                    "objects": {
                        "UserRequest::123": {
                            "fields": {
                                "ref": "R-000123",
                                "title": "Help me",
                                "description": "I need help with my PC",
                                "service_id": 45,
                                "servicesubcategory_id": 67,
                            }
                        }
                    },
                }
            elif class_name == "Service":
                return {
                    "code": 0,
                    "message": "Success",
                    "objects": {"Service::45": {"fields": {"name": "Hardware Support", "description": "PC support"}}},
                }
            elif class_name == "ServiceSubcategory":
                return {
                    "code": 0,
                    "message": "Success",
                    "objects": {"ServiceSubcategory::67": {"fields": {"name": "PC Support", "description": "PC fix"}}},
                }
            return {"code": 0, "objects": {}}

        mock_get_objects.side_effect = side_effect

        payload = {"id": 123, "class": "UserRequest"}

        # Act
        response = self.client.post("/webhook", json=payload)

        # Assert
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["status"], "success")
        self.assertEqual(data["data"]["ref"], "R-000123")
        self.assertEqual(data["data"]["service_details"]["name"], "Hardware Support")
        self.assertEqual(data["data"]["servicesubcategory_details"]["name"], "PC Support")

        self.assertEqual(mock_get_objects.call_count, 3)

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
