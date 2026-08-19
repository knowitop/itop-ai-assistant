import unittest

from itop_ai_assistant.agents.intake.domain import NonBlankText, RoundBudget, check_round_budget, stop_reason
from itop_ai_assistant.agents.intake.state import TicketState
from itop_ai_assistant.domain.ticket import LogEntry, Ticket


def _ticket(status: str = "new", public_log: list[LogEntry] | None = None) -> Ticket:
    return Ticket(obj_class="Incident", id="123", status=status, public_log=public_log or [])


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
