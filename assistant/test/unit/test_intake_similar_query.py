"""What intake means by "a similar solved ticket", checked as a value.

The scenario is configuration turned into a `SearchQuery` (TASK-031), so it
needs no LLM, no iTop and no vector store to pin down — which is the whole
reason `agents/intake/similar.py` exists apart from the tool that calls it.
"""

import unittest
from datetime import UTC, datetime, timedelta

from itop_ai_assistant.agents.intake.config import IntakeConfig
from itop_ai_assistant.agents.intake.similar import similar_query
from itop_ai_assistant.vector import TICKETS_FAMILY

_NOW = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)


def _query(cfg: IntakeConfig | None = None):
    return similar_query(
        cfg or IntakeConfig(),
        text="Printer is dead\n\nCannot print",
        exclude=("Incident", 42),
        now=_NOW,
    )


class TestSimilarQuery(unittest.TestCase):
    def test_the_family_is_the_ticket_index(self):
        self.assertEqual(_query().family, TICKETS_FAMILY)

    def test_only_solved_tickets_are_asked_for(self):
        query = _query(IntakeConfig(resolved_statuses=["closed", "resolved"]))

        self.assertEqual(query.filters, {"status": ["closed", "resolved"]})

    def test_the_asking_ticket_is_excluded(self):
        self.assertEqual(_query().exclude, ("Incident", 42))

    def test_internal_chunks_are_never_searched(self):
        # A safeguard, not a default: TASK-013 puts private log chunks in the
        # index, and intake must not start quoting them without a change here
        self.assertEqual(list(_query().visibilities), ["public"])

    def test_the_age_window_is_a_lower_bound_measured_from_the_given_clock(self):
        query = _query(IntakeConfig(similar_max_age_days=30))

        self.assertIsNone(query.updated.before)
        self.assertEqual(query.updated.after, _NOW - timedelta(days=30))

    def test_no_class_scope_of_its_own(self):
        # Which classes the family covers is the source's business, not
        # intake's — this is not the same as an empty list, which is an error
        self.assertIsNone(_query().classes)

    def test_the_budget_and_the_floor_come_from_the_config(self):
        query = _query(
            IntakeConfig(
                similar_candidates=11,
                similar_top=3,
                similar_min_score=0.42,
                similar_chunk_kinds=["profile", "solution"],
            )
        )

        self.assertEqual(query.candidates, 11)
        self.assertEqual(query.top, 3)
        self.assertEqual(query.min_score, 0.42)
        self.assertEqual(query.chunk_kinds, ["profile", "solution"])


if __name__ == "__main__":
    unittest.main()
