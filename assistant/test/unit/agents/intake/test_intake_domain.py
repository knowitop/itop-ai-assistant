import unittest

from itop_ai_assistant.agents.intake.domain import (
    IntakeScope,
    NonBlankText,
    QuestionBudget,
    check_question_budget,
    closing_tools,
    format_scope,
    needs_classification,
    stop_reason,
)
from itop_ai_assistant.agents.intake.state import TicketState
from itop_ai_assistant.domain.ticket import LogEntry, Ticket


def _ticket(status: str = "new", public_log: list[LogEntry] | None = None) -> Ticket:
    return Ticket(obj_class="Incident", id="123", status=status, public_log=public_log or [])


def _scope(**overrides: bool) -> IntakeScope:
    return IntakeScope(**{"classify": True, "clarify": True, "handoff_note": True, "similar": True, **overrides})


class TestCheckQuestionBudget(unittest.TestCase):
    """Pure over `TicketState` and two ints — no config, no I/O, no framework."""

    @staticmethod
    def _check(state: TicketState, *, classifying: bool) -> QuestionBudget:
        return check_question_budget(state, classifying=classifying, max_questions=3, max_classify_questions=2)

    def test_under_budget_while_classifying(self):
        state = TicketState(questions_asked=1, classify_questions_asked=1)

        self.assertEqual(self._check(state, classifying=True), QuestionBudget.OK)

    def test_classify_sub_limit_exhausted_at_the_threshold(self):
        state = TicketState(questions_asked=2, classify_questions_asked=2)

        self.assertEqual(self._check(state, classifying=True), QuestionBudget.CLASSIFY_EXHAUSTED)

    def test_classify_sub_limit_exhausted_past_the_threshold(self):
        state = TicketState(questions_asked=3, classify_questions_asked=3)

        self.assertEqual(self._check(state, classifying=True), QuestionBudget.CLASSIFY_EXHAUSTED)

    def test_under_budget_once_classified(self):
        state = TicketState(questions_asked=2, classify_questions_asked=2)

        self.assertEqual(self._check(state, classifying=False), QuestionBudget.OK)

    def test_overall_ceiling_exhausted_at_the_threshold(self):
        state = TicketState(questions_asked=3)

        self.assertEqual(self._check(state, classifying=False), QuestionBudget.EXHAUSTED)

    def test_classification_questions_spend_the_overall_budget_too(self):
        # Two questions about the category and one about completeness is the
        # whole ceiling — the phases share it, they do not each get their own
        state = TicketState(questions_asked=3, classify_questions_asked=2)

        self.assertEqual(self._check(state, classifying=False), QuestionBudget.EXHAUSTED)

    def test_the_sub_limit_is_checked_before_the_ceiling(self):
        # Both are spent: the model must be told the category is unknown, not
        # that the ticket has enough detail
        state = TicketState(questions_asked=3, classify_questions_asked=2)

        self.assertEqual(self._check(state, classifying=True), QuestionBudget.CLASSIFY_EXHAUSTED)

    def test_a_sub_limit_equal_to_the_ceiling_leaves_no_reserve(self):
        state = TicketState(questions_asked=2, classify_questions_asked=2)

        result = check_question_budget(state, classifying=False, max_questions=2, max_classify_questions=2)

        self.assertEqual(result, QuestionBudget.EXHAUSTED)


class TestNeedsClassification(unittest.TestCase):
    """Where "the administrator allowed it" meets "the ticket still needs it"."""

    @staticmethod
    def _classified() -> Ticket:
        ticket = _ticket()
        ticket.service_id, ticket.subcategory_id = "10", "101"
        return ticket

    def test_enabled_and_unclassified(self):
        self.assertTrue(needs_classification(_scope(), _ticket()))

    def test_enabled_but_already_classified(self):
        self.assertFalse(needs_classification(_scope(), self._classified()))

    def test_disabled_and_unclassified(self):
        # The stage nobody runs: an empty subcategory is not a reason to start
        self.assertFalse(needs_classification(_scope(classify=False), _ticket()))

    def test_disabled_and_already_classified(self):
        self.assertFalse(needs_classification(_scope(classify=False), self._classified()))

    def test_half_classified_ticket_still_needs_it(self):
        ticket = _ticket()
        ticket.service_id = "10"

        self.assertTrue(needs_classification(_scope(), ticket))


class TestClosingTools(unittest.TestCase):
    def test_both_ways_out(self):
        self.assertEqual(closing_tools(_scope()), ["post_public_question", "finish_handoff"])

    def test_without_the_note_the_silent_finish_stands_in(self):
        self.assertEqual(
            closing_tools(_scope(handoff_note=False, similar=False)),
            ["post_public_question", "finish_processing"],
        )

    def test_a_run_that_only_classifies_still_has_a_way_out(self):
        self.assertEqual(
            closing_tools(_scope(clarify=False, handoff_note=False, similar=False)),
            ["finish_processing"],
        )


class TestFormatScope(unittest.TestCase):
    def test_every_action_is_named_in_a_fixed_order(self):
        text = format_scope(_scope(clarify=False, similar=False))

        self.assertEqual(text, "classify=on clarify=off handoff_note=on similar=off")


class TestNonBlankText(unittest.TestCase):
    def test_non_empty_text_is_accepted(self):
        self.assertEqual(NonBlankText("What broke?").value, "What broke?")

    def test_empty_string_is_rejected(self):
        with self.assertRaises(ValueError):
            NonBlankText("")

    def test_whitespace_only_is_rejected(self):
        with self.assertRaises(ValueError):
            NonBlankText("   \n\t")


class TestStopReason(unittest.TestCase):
    """Pure over `Ticket` + `TicketState` and two primitives — no config, no I/O."""

    def test_ai_done_stops_regardless_of_status_or_log(self):
        result = stop_reason(
            _ticket(status="assigned"), TicketState(ai_done=True), active_statuses=["new"], ai_name="ai-assistant"
        )

        self.assertEqual(result, "already processed (ai_done)")

    def test_inactive_status_stops(self):
        result = stop_reason(_ticket(status="assigned"), TicketState(), active_statuses=["new"], ai_name="ai-assistant")

        self.assertEqual(result, "status=assigned not in ['new']")

    def test_last_public_entry_is_ours_stops(self):
        ticket = _ticket(
            public_log=[
                LogEntry(user_login="John Doe", message="printer is dead"),
                LogEntry(user_login="ai-assistant", message="which printer?"),
            ]
        )

        result = stop_reason(ticket, TicketState(), active_statuses=["new"], ai_name="ai-assistant")

        self.assertEqual(result, "last public entry is ours, waiting for the requester")

    def test_last_public_entry_is_users_proceeds(self):
        ticket = _ticket(
            public_log=[
                LogEntry(user_login="ai-assistant", message="which printer?"),
                LogEntry(user_login="John Doe", message="the one in room 3"),
            ]
        )

        result = stop_reason(ticket, TicketState(), active_statuses=["new"], ai_name="ai-assistant")

        self.assertIsNone(result)

    def test_empty_log_proceeds(self):
        result = stop_reason(_ticket(), TicketState(), active_statuses=["new"], ai_name="ai-assistant")

        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
