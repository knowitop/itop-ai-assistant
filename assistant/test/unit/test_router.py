import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

from fastapi.testclient import TestClient

from main import app


class TestWebhook(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    @patch("router._create_ai_checker")
    @patch("router._create_itop_client")
    def test_webhook_success(self, mock_create_itop, mock_create_checker):
        # Arrange
        mock_itop = MagicMock()
        mock_checker = MagicMock()
        mock_create_itop.return_value = mock_itop
        mock_create_checker.return_value = mock_checker
        mock_checker.check_completeness = AsyncMock()

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

        mock_itop.get_objects.side_effect = side_effect
        mock_checker.check_completeness.return_value = None  # No missing info

        payload = {"id": 123, "class": "UserRequest", "async": False}

        # Act
        response = self.client.post("/webhook", json=payload)

        # Assert
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["status"], "success")
        self.assertIn("processing_id", data)
        try:
            UUID(data["processing_id"])
        except ValueError:
            self.fail("processing_id is not a valid UUID")
        self.assertEqual(data["data"]["ai_check_result"], "OK")
        mock_itop.update_object.assert_not_called()

    @patch("router._create_ai_checker")
    @patch("router._create_itop_client")
    def test_webhook_missing_info(self, mock_create_itop, mock_create_checker):
        # Arrange
        mock_itop = MagicMock()
        mock_checker = MagicMock()
        mock_create_itop.return_value = mock_itop
        mock_create_checker.return_value = mock_checker
        mock_checker.check_completeness = AsyncMock()

        mock_itop.get_objects.side_effect = [
            {
                "code": 0,
                "objects": {"UserRequest::123": {"fields": {"title": "T", "description": "D", "service_id": 1}}},
            },
            {"code": 0, "objects": {"Service::1": {"fields": {"name": "S", "description": "SD"}}}},
            {"code": 0, "objects": {}},  # No subcategory
        ]
        missing_msg = "Please provide your phone number."
        mock_checker.check_completeness.return_value = missing_msg

        payload = {"id": 123, "class": "UserRequest", "async": False}

        # Act
        response = self.client.post("/webhook", json=payload)

        # Assert
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("processing_id", data)
        try:
            UUID(data["processing_id"])
        except ValueError:
            self.fail("processing_id is not a valid UUID")
        self.assertEqual(data["data"]["ai_check_result"], missing_msg)
        mock_itop.update_object.assert_called_once_with(
            class_name="UserRequest",
            key=123,
            fields={"public_log": missing_msg},
            comment="AI assistant check: missing information",
        )

    @patch("router._create_ai_checker")
    @patch("router._create_itop_client")
    def test_webhook_incident_success(self, mock_create_itop, mock_create_checker):
        # Arrange
        mock_itop = MagicMock()
        mock_checker = MagicMock()
        mock_create_itop.return_value = mock_itop
        mock_create_checker.return_value = mock_checker
        mock_checker.check_completeness = AsyncMock()

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

        mock_itop.get_objects.side_effect = side_effect
        mock_checker.check_completeness.return_value = None

        payload = {"id": 456, "class": "Incident", "async": False}

        # Act
        response = self.client.post("/webhook", json=payload)

        # Assert
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("processing_id", data)
        try:
            UUID(data["processing_id"])
        except ValueError:
            self.fail("processing_id is not a valid UUID")
        self.assertEqual(data["status"], "success")
        self.assertEqual(data["data"]["ref"], "I-000456")
        self.assertEqual(data["data"]["ai_check_result"], "OK")

    @patch("router._create_itop_client")
    def test_webhook_not_found(self, mock_create_itop):
        # Arrange
        mock_itop = MagicMock()
        mock_create_itop.return_value = mock_itop
        mock_itop.get_objects.return_value = {"code": 0, "message": "Success", "objects": None}

        payload = {"id": 999, "class": "UserRequest", "async": False}

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

    @patch("router._create_ai_checker")
    @patch("router._create_itop_client")
    def test_webhook_ai_error(self, mock_create_itop, mock_create_checker):
        # Arrange
        mock_itop = MagicMock()
        mock_checker = MagicMock()
        mock_create_itop.return_value = mock_itop
        mock_create_checker.return_value = mock_checker
        mock_checker.check_completeness = AsyncMock()

        mock_itop.get_objects.return_value = {
            "code": 0,
            "objects": {"UserRequest::123": {"fields": {"title": "T", "description": "D"}}},
        }
        mock_checker.check_completeness.side_effect = Exception("AI failure")

        payload = {"id": 123, "class": "UserRequest", "async": False}

        # Act
        response = self.client.post("/webhook", json=payload)

        # Assert
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("processing_id", data)
        try:
            UUID(data["processing_id"])
        except ValueError:
            self.fail("processing_id is not a valid UUID")
        self.assertEqual(data["data"]["ai_check_result"], "Error")

    @patch("router.process_webhook_logic")
    def test_webhook_async_true(self, mock_process_logic):
        # Arrange
        payload = {"id": 123, "class": "UserRequest", "async": True}

        # Act
        response = self.client.post("/webhook", json=payload)

        # Assert
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["status"], "accepted")
        self.assertIn("processing_id", data)
        try:
            UUID(data["processing_id"])
        except ValueError:
            self.fail("processing_id is not a valid UUID")
        self.assertEqual(data["message"], "Webhook processing started in background")
        # In TestClient, background tasks are usually executed immediately
        mock_process_logic.assert_called_once()

    @patch("router._create_ai_checker")
    @patch("router._create_itop_client")
    def test_webhook_async_false(self, mock_create_itop, mock_create_checker):
        # Arrange
        mock_itop = MagicMock()
        mock_checker = MagicMock()
        mock_create_itop.return_value = mock_itop
        mock_create_checker.return_value = mock_checker
        mock_checker.check_completeness = AsyncMock()

        mock_itop.get_objects.return_value = {
            "code": 0,
            "objects": {"UserRequest::123": {"fields": {"title": "T", "description": "D"}}},
        }
        mock_checker.check_completeness.return_value = None
        payload = {"id": 123, "class": "UserRequest", "async": False}

        # Act
        response = self.client.post("/webhook", json=payload)

        # Assert
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["status"], "success")
        self.assertIn("processing_id", data)
        try:
            UUID(data["processing_id"])
        except ValueError:
            self.fail("processing_id is not a valid UUID")
        self.assertEqual(data["data"]["ai_check_result"], "OK")

    @patch("router.process_webhook_logic")
    def test_webhook_async_default(self, mock_process_logic):
        # Arrange
        # By default async should be True
        payload = {"id": 123, "class": "UserRequest"}

        # Act
        response = self.client.post("/webhook", json=payload)

        # Assert
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("processing_id", data)
        try:
            UUID(data["processing_id"])
        except ValueError:
            self.fail("processing_id is not a valid UUID")
        self.assertEqual(data["status"], "accepted")
        mock_process_logic.assert_called_once()


if __name__ == "__main__":
    unittest.main()
