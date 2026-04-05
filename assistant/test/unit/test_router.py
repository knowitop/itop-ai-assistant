import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

from fastapi.testclient import TestClient
from graph.graph import Action

from main import app


def _make_schema_mock(find_return=None, update_return=None):
    """Create a mock Schema with async find() and update() methods."""
    m = MagicMock()
    m.find = AsyncMock(return_value=find_return)
    m.update = AsyncMock(return_value=update_return or [])
    return m


def _setup_itop_mock(mock_create_itop, schemas: dict):
    """
    Wire up mock_itop so that itop.schema(name) returns the right per-class mock.
    schemas: {"ClassName": schema_mock, ...}
    """
    mock_itop = MagicMock()
    mock_itop.schema.side_effect = lambda name: schemas.get(name, _make_schema_mock())
    mock_create_itop.return_value = mock_itop
    return mock_itop


class TestWebhook(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    @patch("router.evaluate_node", new_callable=AsyncMock)
    @patch("router._create_itop_client")
    def test_webhook_success(self, mock_create_itop, mock_evaluate):
        schemas = {
            "UserRequest": _make_schema_mock(
                find_return={
                    "id": "123",
                    "ref": "R-000123",
                    "title": "Help me",
                    "description": "I need help with my PC",
                    "service_id": 45,
                    "servicesubcategory_id": 67,
                }
            ),
            "Service": _make_schema_mock(find_return={"name": "Hardware Support", "description": "PC support"}),
            "ServiceSubcategory": _make_schema_mock(find_return={"name": "PC Support", "description": "PC fix"}),
        }
        _setup_itop_mock(mock_create_itop, schemas)
        mock_evaluate.return_value = {"action": Action.ENRICH}

        response = self.client.post("/webhook", json={"id": 123, "class": "UserRequest", "async": False})

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["status"], "success")
        self.assertIn("processing_id", data)
        UUID(data["processing_id"])
        self.assertEqual(data["data"]["ai_check_result"], "OK")
        schemas["UserRequest"].update.assert_not_called()

    @patch("router._create_state_manager")
    @patch("router.evaluate_node", new_callable=AsyncMock)
    @patch("router._create_itop_client")
    def test_webhook_missing_info(self, mock_create_itop, mock_evaluate, mock_create_state_manager):
        schemas = {
            "UserRequest": _make_schema_mock(
                find_return={
                    "id": "123",
                    "ref": "R-000123",
                    "title": "T",
                    "description": "D",
                    "service_id": 1,
                }
            ),
            "Service": _make_schema_mock(find_return={"name": "S", "description": "SD"}),
            "ServiceSubcategory": _make_schema_mock(find_return=None),
        }
        _setup_itop_mock(mock_create_itop, schemas)

        missing_msg = "Please provide your phone number."
        mock_evaluate.return_value = {"action": Action.ASK, "question": missing_msg}

        mock_state = MagicMock()
        mock_state.increment_rounds = AsyncMock()
        mock_create_state_manager.return_value = mock_state

        response = self.client.post("/webhook", json={"id": 123, "class": "UserRequest", "async": False})

        self.assertEqual(response.status_code, 200)
        data = response.json()
        UUID(data["processing_id"])
        self.assertEqual(data["data"]["ai_check_result"], missing_msg)
        schemas["UserRequest"].update.assert_called_once()
        call_args = schemas["UserRequest"].update.call_args
        self.assertEqual(call_args[0][0], {"id": 123})
        self.assertEqual(call_args[0][1]["public_log"]["add_item"]["message"], missing_msg)
        mock_state.increment_rounds.assert_called_once_with("R-000123")

    @patch("router.evaluate_node", new_callable=AsyncMock)
    @patch("router._create_itop_client")
    def test_webhook_incident_success(self, mock_create_itop, mock_evaluate):
        schemas = {
            "Incident": _make_schema_mock(
                find_return={
                    "id": "456",
                    "ref": "I-000456",
                    "title": "Server down",
                    "description": "Production server is unreachable",
                    "service_id": 10,
                    "servicesubcategory_id": 20,
                }
            ),
            "Service": _make_schema_mock(find_return={"name": "Infrastructure", "description": "Server support"}),
            "ServiceSubcategory": _make_schema_mock(find_return={"name": "Server fix", "description": "Hardware fix"}),
        }
        _setup_itop_mock(mock_create_itop, schemas)
        mock_evaluate.return_value = {"action": Action.ENRICH}

        response = self.client.post("/webhook", json={"id": 456, "class": "Incident", "async": False})

        self.assertEqual(response.status_code, 200)
        data = response.json()
        UUID(data["processing_id"])
        self.assertEqual(data["status"], "success")
        self.assertEqual(data["data"]["ref"], "I-000456")
        self.assertEqual(data["data"]["ai_check_result"], "OK")

    @patch("router._create_itop_client")
    def test_webhook_not_found(self, mock_create_itop):
        schemas = {"UserRequest": _make_schema_mock(find_return=[])}
        _setup_itop_mock(mock_create_itop, schemas)

        response = self.client.post("/webhook", json={"id": 999, "class": "UserRequest", "async": False})

        self.assertEqual(response.status_code, 404)
        self.assertIn("not found", response.json()["detail"])

    def test_webhook_invalid_payload(self):
        response = self.client.post("/webhook", json={"wrong": "payload"})
        self.assertEqual(response.status_code, 422)

    @patch("router.evaluate_node", new_callable=AsyncMock)
    @patch("router._create_itop_client")
    def test_webhook_ai_error(self, mock_create_itop, mock_evaluate):
        schemas = {
            "UserRequest": _make_schema_mock(find_return={"id": "123", "title": "T", "description": "D"}),
        }
        _setup_itop_mock(mock_create_itop, schemas)
        mock_evaluate.side_effect = Exception("AI failure")

        response = self.client.post("/webhook", json={"id": 123, "class": "UserRequest", "async": False})

        self.assertEqual(response.status_code, 200)
        data = response.json()
        UUID(data["processing_id"])
        self.assertEqual(data["data"]["ai_check_result"], "Error")

    @patch("router.process_webhook_logic")
    def test_webhook_async_true(self, mock_process_logic):
        response = self.client.post("/webhook", json={"id": 123, "class": "UserRequest", "async": True})

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["status"], "accepted")
        UUID(data["processing_id"])
        self.assertEqual(data["message"], "Webhook processing started in background")
        mock_process_logic.assert_called_once()

    @patch("router.evaluate_node", new_callable=AsyncMock)
    @patch("router._create_itop_client")
    def test_webhook_async_false(self, mock_create_itop, mock_evaluate):
        schemas = {
            "UserRequest": _make_schema_mock(find_return={"id": "123", "title": "T", "description": "D"}),
        }
        _setup_itop_mock(mock_create_itop, schemas)
        mock_evaluate.return_value = {"action": Action.ENRICH}

        response = self.client.post("/webhook", json={"id": 123, "class": "UserRequest", "async": False})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "success")

    @patch("router.process_webhook_logic")
    def test_webhook_async_default(self, mock_process_logic):
        response = self.client.post("/webhook", json={"id": 123, "class": "UserRequest"})

        self.assertEqual(response.status_code, 200)
        data = response.json()
        UUID(data["processing_id"])
        self.assertEqual(data["status"], "accepted")
        mock_process_logic.assert_called_once()


if __name__ == "__main__":
    unittest.main()
