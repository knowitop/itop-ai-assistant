"""`SearchQuery` validates the scenario where it is built (TASK-031).

A scenario comes from configuration an administrator edits, so the useful
place to reject a bad one is at construction — not halfway through a run over
a real ticket. The empty-list rules are ADR-017's convention ("unrestricted"
is the absent argument) restated one layer above `ChunkStore.search()`, which
enforces the same on its own arguments.
"""

import unittest

from itop_ai_assistant.vector.ports.query import SearchQuery


def _query(**overrides) -> SearchQuery:
    return SearchQuery(**{"text": "q", "family": "tickets", **overrides})


class TestSearchQuery(unittest.TestCase):
    def test_a_plain_scenario_is_accepted(self):
        query = _query(classes=["UserRequest"], filters={"status": ["resolved"]})

        self.assertEqual(query.candidates, 15)
        self.assertEqual(query.top, 5)
        self.assertEqual(list(query.visibilities), ["public", "internal"])

    def test_empty_text_is_not_an_error(self):
        # "nothing to match on" is a normal state of a ticket, answered with
        # no hits — unlike an empty list, which is a mistake in the scenario
        self.assertEqual(_query(text="").text, "")

    def test_empty_classes_is_rejected(self):
        with self.assertRaises(ValueError):
            _query(classes=[])

    def test_empty_chunk_kinds_is_rejected(self):
        with self.assertRaises(ValueError):
            _query(chunk_kinds=[])

    def test_a_filter_key_with_no_values_is_rejected(self):
        with self.assertRaises(ValueError):
            _query(filters={"status": []})

    def test_min_score_outside_the_cosine_range_is_rejected(self):
        with self.assertRaises(ValueError):
            _query(min_score=1.5)

    def test_min_score_may_be_negative(self):
        # Cosine similarity is [-1, 1]; a negative floor is unusual, not wrong
        self.assertEqual(_query(min_score=-0.2).min_score, -0.2)

    def test_zero_candidates_or_top_is_rejected(self):
        with self.assertRaises(ValueError):
            _query(candidates=0)
        with self.assertRaises(ValueError):
            _query(top=0)

    def test_top_above_candidates_is_rejected(self):
        # The source only ever drops candidates (ADR-003), so asking for more
        # results than candidates silently caps at `candidates` — the
        # configured `top` would do nothing at all
        with self.assertRaises(ValueError):
            _query(candidates=5, top=10)

    def test_top_equal_to_candidates_is_allowed(self):
        # "expect nothing to be dropped" is a legitimate scenario, unlike
        # "expect more back than was asked for"
        self.assertEqual(_query(candidates=5, top=5).top, 5)
