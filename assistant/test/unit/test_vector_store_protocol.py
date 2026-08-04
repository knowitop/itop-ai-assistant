"""The port is a contract, and a contract nobody checks is a comment."""

import unittest

from itop_ai_assistant.vector.db import VectorDb
from itop_ai_assistant.vector.index import VectorIndex
from itop_ai_assistant.vector.store import ChunkStore


class TestChunkStoreProtocol(unittest.TestCase):
    def test_pgvector_implementation_satisfies_the_port(self):
        # A real (unconfigured) VectorDb, not None: `configured` is a property,
        # and isinstance() against a runtime_checkable Protocol calls it.
        self.assertIsInstance(VectorIndex(VectorDb(None)), ChunkStore)

    def test_port_does_not_leak_sync_state(self):
        # Cursors, the reindex flag and the journal are operational state and
        # belong in Redis — a backend that grows them back has broken the seam.
        forbidden = {
            "get_cursor",
            "set_cursor",
            "list_cursors",
            "reset_cursors",
            "request_reindex",
            "reindex_pending",
            "journal_start",
            "journal_finish",
            "journal_recent",
            "try_advisory_lock",
        }

        self.assertEqual(forbidden & set(ChunkStore.__protocol_attrs__), set())
