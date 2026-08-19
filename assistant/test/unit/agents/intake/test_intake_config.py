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


class TestActionToggles(unittest.TestCase):
    def test_every_action_is_on_by_default(self):
        # A deployment that never touched the settings must not notice them
        cfg = IntakeConfig()

        self.assertEqual(
            (cfg.classify_enabled, cfg.clarify_enabled, cfg.handoff_note_enabled, cfg.similar_enabled),
            (True, True, True, True),
        )

    def test_similar_tickets_without_a_handoff_note_is_rejected(self):
        # The references live only inside the note — there is nothing else to
        # enrich with them
        with self.assertRaises(ValidationError) as ctx:
            IntakeConfig(handoff_note_enabled=False)

        self.assertIn("handoff_note_enabled", str(ctx.exception))

    def test_similar_may_be_switched_off_together_with_the_note(self):
        cfg = IntakeConfig(handoff_note_enabled=False, similar_enabled=False)

        self.assertFalse(cfg.handoff_note_enabled)

    def test_switching_off_everything_points_at_enabled_instead(self):
        with self.assertRaises(ValidationError) as ctx:
            IntakeConfig(
                classify_enabled=False,
                clarify_enabled=False,
                handoff_note_enabled=False,
                similar_enabled=False,
            )

        self.assertIn("intake.enabled", str(ctx.exception))

    def test_one_action_left_on_is_enough(self):
        cfg = IntakeConfig(clarify_enabled=False, handoff_note_enabled=False, similar_enabled=False)

        self.assertTrue(cfg.classify_enabled)


if __name__ == "__main__":
    unittest.main()
