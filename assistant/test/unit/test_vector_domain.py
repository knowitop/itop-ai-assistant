import unittest
from datetime import UTC, datetime, timedelta

from itop_ai_assistant.vector.domain import (
    ChunkSyncState,
    classify_chunk,
    creation_date,
    left_indexable_scope,
)

_NOW = datetime(2026, 7, 10, 12, 0, tzinfo=UTC)


class TestLeftIndexableScope(unittest.TestCase):
    def test_value_outside_the_list_has_left(self):
        self.assertTrue(left_indexable_scope("new", ["resolved", "closed"]))

    def test_value_inside_the_list_stays(self):
        self.assertFalse(left_indexable_scope("resolved", ["resolved", "closed"]))

    def test_empty_list_means_index_everything(self):
        self.assertFalse(left_indexable_scope("new", []))

    def test_an_unknown_value_leaves_nothing(self):
        # A deployment that maps no lifecycle state reads every object the
        # same way; taking that for "outside the list" empties the class.
        self.assertFalse(left_indexable_scope(None, ["resolved", "closed"]))


class TestClassifyChunk(unittest.TestCase):
    def test_no_stored_digest_is_changed(self):
        self.assertEqual(
            classify_chunk("c1", "m1", stored_content_hash=None, stored_meta_hash=None), ChunkSyncState.CHANGED
        )

    def test_content_hash_mismatch_is_changed_even_if_meta_also_differs(self):
        self.assertEqual(
            classify_chunk("c2", "m2", stored_content_hash="c1", stored_meta_hash="m1"), ChunkSyncState.CHANGED
        )

    def test_meta_hash_mismatch_alone_is_stale_meta(self):
        self.assertEqual(
            classify_chunk("c1", "m2", stored_content_hash="c1", stored_meta_hash="m1"), ChunkSyncState.STALE_META
        )

    def test_both_match_is_unchanged(self):
        self.assertEqual(
            classify_chunk("c1", "m1", stored_content_hash="c1", stored_meta_hash="m1"), ChunkSyncState.UNCHANGED
        )


class TestCreationDate(unittest.TestCase):
    def test_the_records_own_date_wins(self):
        own = _NOW - timedelta(days=1)
        self.assertEqual(creation_date(own, None, [], _NOW), own)

    def test_falls_back_to_the_earliest_stored_date(self):
        earliest = _NOW - timedelta(days=10)
        stored = [_NOW - timedelta(days=2), earliest, None]
        self.assertEqual(creation_date(None, None, stored, _NOW), earliest)

    def test_falls_back_to_updated_at_when_nothing_stored(self):
        updated = _NOW - timedelta(days=5)
        self.assertEqual(creation_date(None, updated, [], _NOW), updated)

    def test_falls_back_to_started_at_as_the_last_resort(self):
        self.assertEqual(creation_date(None, None, [], _NOW), _NOW)
