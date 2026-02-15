import unittest
from unittest.mock import patch

from agent import ITopInfoChecker


class TestAgent(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.model_name = "google_genai:gemini-1.5-flash"
        with patch.dict("os.environ", {"GOOGLE_API_KEY": "fake-key"}):
            self.checker = ITopInfoChecker(model_name=self.model_name)

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
        mock_invoke.return_value = "Missing information: Phone number"
        # Act
        result = await self.checker.check_completeness("Title", "Description", "Service", "Subcategory")
        # Assert
        self.assertEqual(result, "Missing information: Phone number")

    @patch("agent.init_chat_model")
    def test_provider_initialization(self, mock_init):
        # Test Google
        model_google = "google_genai:gemini-1.5-flash"
        with patch.dict("os.environ", {"GOOGLE_API_KEY": "google-key"}):
            checker = ITopInfoChecker(model_name=model_google)
            self.assertEqual(checker.model_name, model_google)
            mock_init.assert_called_with(model=model_google)

        # Test OpenAI
        model_openai = "openai:gpt-4o-mini"
        with patch.dict("os.environ", {"OPENAI_API_KEY": "openai-key"}):
            checker = ITopInfoChecker(model_name=model_openai)
            self.assertEqual(checker.model_name, model_openai)
            mock_init.assert_called_with(model=model_openai)

    @patch("agent.init_chat_model")
    def test_invalid_model(self, mock_init):
        mock_init.side_effect = Exception("Unsupported model")
        with self.assertRaises(Exception):
            ITopInfoChecker(model_name="unknown:model")

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
