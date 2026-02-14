import json
import unittest
from unittest.mock import MagicMock, patch

from itop.client import ITopClient


class TestITopClient(unittest.TestCase):
    def setUp(self):
        # Arrange
        self.client = ITopClient(url="http://itop/webservices/rest.php", auth_user="admin", auth_pwd="password")

    @patch("httpx.post")
    def test_get_objects(self, mock_post):
        # Arrange
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "code": 0,
            "message": "Success",
            "objects": {"UserRequest::1": {"fields": {"id": 1}}},
        }
        mock_post.return_value = mock_response

        # Act
        result = self.client.get_objects("UserRequest", 1, output_fields=["id", "friendlyname"])

        # Assert
        self.assertEqual(result["code"], 0)
        mock_post.assert_called_once()

        # Verify request data
        args, kwargs = mock_post.call_args
        data = kwargs["data"]
        self.assertEqual(data["auth_user"], "admin")
        self.assertEqual(data["auth_pwd"], "password")

        json_data = json.loads(data["json_data"])
        self.assertEqual(json_data["operation"], "core/get")
        self.assertEqual(json_data["class"], "UserRequest")
        self.assertEqual(json_data["key"], 1)
        self.assertEqual(json_data["output_fields"], "id,friendlyname")

    @patch("httpx.post")
    def test_update_object(self, mock_post):
        # Arrange
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"code": 0, "message": "Updated"}
        mock_post.return_value = mock_response

        # Act
        result = self.client.update_object(
            "UserRequest", 1, {"status": "closed"}, "Closing it", output_fields=["status", "org_id"]
        )

        # Assert
        self.assertEqual(result["code"], 0)

        args, kwargs = mock_post.call_args
        json_data = json.loads(kwargs["data"]["json_data"])
        self.assertEqual(json_data["operation"], "core/update")
        self.assertEqual(json_data["fields"], {"status": "closed"})
        self.assertEqual(json_data["comment"], "Closing it")
        self.assertEqual(json_data["output_fields"], "status,org_id")

    @patch("httpx.post")
    def test_get_objects_with_token(self, mock_post):
        # Arrange
        client_token = ITopClient(url="http://itop/webservices/rest.php", auth_token="secret-token")
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"code": 0, "message": "Success", "objects": {}}
        mock_post.return_value = mock_response

        # Act
        client_token.get_objects("UserRequest", 1)

        # Assert
        args, kwargs = mock_post.call_args
        self.assertEqual(kwargs["headers"]["Auth-Token"], "secret-token")
        self.assertNotIn("auth_user", kwargs["data"])
        self.assertNotIn("auth_pwd", kwargs["data"])

    @patch("httpx.post")
    def test_get_objects_pagination(self, mock_post):
        # Arrange
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"code": 0, "message": "Success", "objects": {}}
        mock_post.return_value = mock_response

        # Act
        self.client.get_objects("UserRequest", "SELECT UserRequest", limit=10, page=2)

        # Assert
        args, kwargs = mock_post.call_args
        json_data = json.loads(kwargs["data"]["json_data"])
        self.assertEqual(json_data["limit"], 10)
        self.assertEqual(json_data["page"], 2)

    @patch("httpx.post")
    def test_error_handling(self, mock_post):
        # Arrange
        mock_response = MagicMock()
        mock_response.json.return_value = {"code": 100, "message": "Error occurred"}
        mock_post.return_value = mock_response

        # Act & Assert
        with self.assertRaises(Exception) as cm:
            self.client.get_objects("UserRequest", 1)

        self.assertIn("iTop API Error 100", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
