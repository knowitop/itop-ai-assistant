import unittest
from unittest.mock import patch

from pydantic import SecretStr

from agent import ITopInfoChecker

BASE_URL = "http://localhost:1234/v1"
MODEL = "qwen2.5-7b-instruct"


class TestAgent(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        with patch("agent.ChatOpenAI"):
            self.checker = ITopInfoChecker(model_name=MODEL, base_url=BASE_URL)

    @patch("langchain_core.runnables.base.RunnableSequence.ainvoke")
    async def test_check_completeness_ok(self, mock_invoke):
        # Arrange
        mock_invoke.return_value = "OK"
        # Act
        result = await self.checker.check_completeness("Title", "Description", "Service", "Subcategory")
        # Assert
        self.assertIsNone(result)

    @patch("langchain_core.runnables.base.RunnableSequence.ainvoke")
    async def test_check_completeness_missing(self, mock_invoke):
        # Arrange
        missing_msg = "Please provide your phone number for further assistance."
        mock_invoke.return_value = missing_msg
        # Act
        result = await self.checker.check_completeness("Title", "Description", "Service", "Subcategory")
        # Assert
        self.assertEqual(result, missing_msg)

    @patch("agent.ChatOpenAI")
    def test_provider_initialization(self, mock_chat_openai):
        checker = ITopInfoChecker(model_name=MODEL, base_url=BASE_URL, api_key=SecretStr("test-key"))
        self.assertEqual(checker.model_name, MODEL)
        self.assertEqual(checker.base_url, BASE_URL)
        mock_chat_openai.assert_called_with(model=MODEL, base_url=BASE_URL, api_key=SecretStr("test-key"))

    @patch("agent.ChatOpenAI")
    def test_invalid_model(self, mock_chat_openai):
        mock_chat_openai.side_effect = Exception("Connection refused")
        with self.assertRaises(Exception):
            ITopInfoChecker(model_name="nonexistent-model", base_url=BASE_URL)

    @patch("langchain_core.runnables.base.RunnableSequence.ainvoke")
    async def test_check_completeness_error(self, mock_invoke):
        # Arrange
        mock_invoke.side_effect = Exception("LLM Error")
        # Act & Assert
        with self.assertRaises(Exception) as cm:
            await self.checker.check_completeness("Title", "Description", "Service", "Subcategory")
        self.assertEqual(str(cm.exception), "LLM Error")


if __name__ == "__main__":
    unittest.main()
