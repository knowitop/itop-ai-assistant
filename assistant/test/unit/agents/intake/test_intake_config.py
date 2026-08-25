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


class TestUnclassifiedServices(unittest.TestCase):
    """A name typed here would save cleanly and never match anything."""

    def test_declaring_nothing_is_the_default(self):
        self.assertEqual(IntakeConfig().unclassified_service_ids, [])

    def test_ids_are_kept(self):
        self.assertEqual(IntakeConfig(unclassified_service_ids=["7", "12"]).unclassified_service_ids, ["7", "12"])

    def test_surrounding_spaces_are_trimmed(self):
        self.assertEqual(IntakeConfig(unclassified_service_ids=[" 7 "]).unclassified_service_ids, ["7"])

    def test_a_service_name_is_rejected_with_where_to_find_the_id(self):
        with self.assertRaises(ValidationError) as ctx:
            IntakeConfig(unclassified_service_ids=["Mail request"])

        self.assertIn("address bar", str(ctx.exception))

    def test_an_empty_string_is_rejected(self):
        with self.assertRaises(ValidationError):
            IntakeConfig(unclassified_service_ids=[" "])


class TestQuestionBudget(unittest.TestCase):
    def test_defaults_leave_the_completeness_phase_one_question(self):
        cfg = IntakeConfig()

        self.assertEqual((cfg.max_questions, cfg.max_classify_questions), (3, 2))

    def test_a_sub_limit_above_the_ceiling_is_rejected(self):
        with self.assertRaises(ValidationError) as ctx:
            IntakeConfig(max_questions=2, max_classify_questions=3)

        self.assertIn("max_classify_questions", str(ctx.exception))

    def test_a_sub_limit_equal_to_the_ceiling_is_allowed(self):
        # "No reserve for the completeness phase" is a choice, not a mistake
        cfg = IntakeConfig(max_questions=2, max_classify_questions=2)

        self.assertEqual(cfg.max_classify_questions, 2)

    def test_zero_questions_is_not_how_the_action_is_switched_off(self):
        with self.assertRaises(ValidationError):
            IntakeConfig(max_questions=0)


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
