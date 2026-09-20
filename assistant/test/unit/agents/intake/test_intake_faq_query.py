"""What intake means by "a relevant FAQ article", checked as a value.

The scenario is configuration turned into a `SearchQuery` (TASK-031), so it
needs no LLM, no iTop and no vector store to pin down — which is the whole
reason `agents/intake/faq.py` exists apart from the tool that calls it.
"""

import unittest

from itop_ai_assistant.agents.intake.config import IntakeConfig
from itop_ai_assistant.agents.intake.faq import faq_query


def _query(cfg: IntakeConfig | None = None, org_ids: list[str] | None = None):
    return faq_query(cfg or IntakeConfig(), text="Printer is dead\n\nCannot print", org_ids=org_ids)


class TestFaqQuery(unittest.TestCase):
    def test_the_family_is_the_faq_index(self):
        self.assertEqual(_query().family, "faq")

    def test_the_family_comes_from_the_config(self):
        query = _query(IntakeConfig(faq_family="kb_articles"))

        self.assertEqual(query.family, "kb_articles")

    def test_no_status_filter_by_default(self):
        # Stock iTop's FAQ carries no status at all (`domain/faq_schema.py`),
        # so there is nothing to filter by out of the box
        self.assertIsNone(_query().filters)

    def test_a_mapped_status_can_be_filtered(self):
        query = _query(IntakeConfig(faq_statuses=["validated"]))

        self.assertEqual(query.filters, {"status": ["validated"]})

    def test_no_exclusion(self):
        # The ticket being processed is never itself an FAQ article
        self.assertIsNone(_query().exclude)

    def test_internal_chunks_are_never_searched(self):
        self.assertEqual(list(_query().visibilities), ["public"])

    def test_no_age_window(self):
        # Unlike `similar_query`: stock iTop's FAQ carries no date at all,
        # and an article going stale is not the same notion as a solved
        # ticket going stale
        self.assertIsNone(_query().updated)

    def test_the_ticket_organization_scopes_the_search(self):
        # ADR-033: an article naming organizations passes only for one of
        # them, an article naming none passes for everybody
        self.assertEqual(_query(org_ids=["3"]).org_ids, ["3"])

    def test_a_ticket_without_an_organization_is_not_pre_filtered(self):
        # An empty list is always a mistake in a `SearchQuery`; "unrestricted"
        # is expressed by omitting the restriction
        self.assertIsNone(_query().org_ids)

    def test_no_class_scope_of_its_own(self):
        self.assertIsNone(_query().classes)

    def test_the_budget_and_the_floor_come_from_the_config(self):
        query = _query(
            IntakeConfig(
                faq_candidates=11,
                faq_top=3,
                faq_min_score=0.42,
                faq_chunk_kinds=["profile"],
            )
        )

        self.assertEqual(query.candidates, 11)
        self.assertEqual(query.top, 3)
        self.assertEqual(query.min_score, 0.42)
        self.assertEqual(query.chunk_kinds, ["profile"])


if __name__ == "__main__":
    unittest.main()
