import unittest

from pydantic import ValidationError

from itop_ai_assistant.agents.intake.config import IntakeConfig


class TestIntakeConfig(unittest.TestCase):
    def test_default_active_statuses(self):
        self.assertEqual(IntakeConfig().active_statuses, ["new"])

    def test_similar_chunk_kinds_default(self):
        self.assertEqual(IntakeConfig().similar_chunk_kinds, ["profile", "body"])

    def test_similar_chunk_kinds_rejects_empty_list(self):
        with self.assertRaises(ValidationError):
            IntakeConfig(similar_chunk_kinds=[])


if __name__ == "__main__":
    unittest.main()
