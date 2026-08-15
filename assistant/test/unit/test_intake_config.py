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

    def test_asking_for_more_results_than_candidates_is_rejected(self):
        # Candidates are only ever dropped by the requester's own iTop
        # (ADR-003), never added — a `similar_top` above `similar_candidates`
        # would silently do nothing. Caught at save time (422 from the admin
        # API), not mid-run.
        with self.assertRaises(ValidationError):
            IntakeConfig(similar_candidates=5, similar_top=10)

    def test_expecting_nothing_to_be_dropped_is_allowed(self):
        self.assertEqual(IntakeConfig(similar_candidates=5, similar_top=5).similar_top, 5)


if __name__ == "__main__":
    unittest.main()
