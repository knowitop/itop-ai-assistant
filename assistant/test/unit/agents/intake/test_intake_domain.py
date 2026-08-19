import unittest

from itop_ai_assistant.agents.intake.domain import NonBlankText, RoundBudget, check_round_budget
from itop_ai_assistant.agents.intake.state import TicketState


class TestCheckRoundBudget(unittest.TestCase):
    """Pure over `TicketState` and two ints — no config, no I/O, no framework."""

    def test_under_budget_while_classifying(self):
        state = TicketState(classify_rounds=1)

        result = check_round_budget(state, classifying=True, max_rounds=2, max_classify_rounds=2)

        self.assertEqual(result, RoundBudget.OK)

    def test_classify_budget_exhausted_at_the_threshold(self):
        state = TicketState(classify_rounds=2)

        result = check_round_budget(state, classifying=True, max_rounds=2, max_classify_rounds=2)

        self.assertEqual(result, RoundBudget.CLASSIFY_EXHAUSTED)

    def test_classify_budget_exhausted_past_the_threshold(self):
        state = TicketState(classify_rounds=3)

        result = check_round_budget(state, classifying=True, max_rounds=2, max_classify_rounds=2)

        self.assertEqual(result, RoundBudget.CLASSIFY_EXHAUSTED)

    def test_under_budget_once_classified(self):
        state = TicketState(rounds=1)

        result = check_round_budget(state, classifying=False, max_rounds=2, max_classify_rounds=2)

        self.assertEqual(result, RoundBudget.OK)

    def test_rounds_budget_exhausted_at_the_threshold(self):
        state = TicketState(rounds=2)

        result = check_round_budget(state, classifying=False, max_rounds=2, max_classify_rounds=2)

        self.assertEqual(result, RoundBudget.EXHAUSTED)

    def test_classify_rounds_do_not_count_once_classified(self):
        # Two separate counters — spending one must not exhaust the other
        state = TicketState(classify_rounds=5, rounds=0)

        result = check_round_budget(state, classifying=False, max_rounds=2, max_classify_rounds=2)

        self.assertEqual(result, RoundBudget.OK)

    def test_rounds_do_not_count_while_classifying(self):
        state = TicketState(rounds=5, classify_rounds=0)

        result = check_round_budget(state, classifying=True, max_rounds=2, max_classify_rounds=2)

        self.assertEqual(result, RoundBudget.OK)


class TestNonBlankText(unittest.TestCase):
    def test_non_empty_text_is_accepted(self):
        self.assertEqual(NonBlankText("What broke?").value, "What broke?")

    def test_empty_string_is_rejected(self):
        with self.assertRaises(ValueError):
            NonBlankText("")

    def test_whitespace_only_is_rejected(self):
        with self.assertRaises(ValueError):
            NonBlankText("   \n\t")


if __name__ == "__main__":
    unittest.main()
