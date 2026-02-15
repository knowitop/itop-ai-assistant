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

    @patch("webhook.checker.check_completeness")
    @patch("webhook.itop_client.update_object")
    @patch("webhook.itop_client.get_objects")
    def test_webhook_success(self, mock_get_objects, mock_update_object, mock_check_completeness):
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
        mock_check_completeness.return_value = None  # No missing info

        payload = {"id": 123, "class": "UserRequest"}

        # Act
        response = self.client.post("/webhook", json=payload)

        # Assert
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["status"], "success")
        self.assertEqual(data["data"]["ai_check_result"], "OK")
        mock_update_object.assert_not_called()

    @patch("webhook.checker.check_completeness")
    @patch("webhook.itop_client.update_object")
    @patch("webhook.itop_client.get_objects")
    def test_webhook_missing_info(self, mock_get_objects, mock_update_object, mock_check_completeness):
        # Arrange
        mock_get_objects.side_effect = [
            {
                "code": 0,
                "objects": {"UserRequest::123": {"fields": {"title": "T", "description": "D", "service_id": 1}}},
            },
            {"code": 0, "objects": {"Service::1": {"fields": {"name": "S", "description": "SD"}}}},
            {"code": 0, "objects": {}},  # No subcategory
        ]
        mock_check_completeness.return_value = "Missing: Phone"

        payload = {"id": 123, "class": "UserRequest"}

        # Act
        response = self.client.post("/webhook", json=payload)

        # Assert
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["data"]["ai_check_result"], "Missing: Phone")
        mock_update_object.assert_called_once_with(
            class_name="UserRequest",
            key=123,
            fields={"public_log": "Missing: Phone"},
            comment="AI assistant check: missing information",
        )

    @patch("webhook.checker.check_completeness")
    @patch("webhook.itop_client.update_object")
    @patch("webhook.itop_client.get_objects")
    def test_webhook_incident_success(self, mock_get_objects, mock_update_object, mock_check_completeness):
        # Arrange
        def side_effect(class_name, key, output_fields):
            if class_name == "Incident":
                return {
                    "code": 0,
                    "message": "Success",
                    "objects": {
                        "Incident::456": {
                            "fields": {
                                "ref": "I-000456",
                                "title": "Server down",
                                "description": "Production server is unreachable",
                                "service_id": 10,
                                "servicesubcategory_id": 20,
                            }
                        }
                    },
                }
            elif class_name == "Service":
                return {
                    "code": 0,
                    "message": "Success",
                    "objects": {
                        "Service::10": {"fields": {"name": "Infrastructure Support", "description": "Server support"}}
                    },
                }
            elif class_name == "ServiceSubcategory":
                return {
                    "code": 0,
                    "message": "Success",
                    "objects": {
                        "ServiceSubcategory::20": {"fields": {"name": "Server fix", "description": "Hardware fix"}}
                    },
                }
            return {"code": 0, "objects": {}}

        mock_get_objects.side_effect = side_effect
        mock_check_completeness.return_value = None

        payload = {"id": 456, "class": "Incident"}

        # Act
        response = self.client.post("/webhook", json=payload)

        # Assert
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["status"], "success")
        self.assertEqual(data["data"]["ref"], "I-000456")
        self.assertEqual(data["data"]["ai_check_result"], "OK")

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
