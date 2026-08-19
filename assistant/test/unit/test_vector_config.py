import unittest

from itop_ai_assistant.vector.config import VectorConfig


class TestVectorConfig(unittest.TestCase):
    def test_disabled_by_default(self):
        cfg = VectorConfig()
        self.assertFalse(cfg.enabled)
        self.assertEqual(list(cfg.families), ["tickets", "faq"])
        tickets = cfg.families["tickets"].classes
        self.assertEqual(list(tickets), ["UserRequest", "Incident"])
        self.assertEqual(tickets["UserRequest"].index_values, ["resolved", "closed"])
        self.assertEqual(tickets["UserRequest"].chunks["body"].fields, ["description"])
        faq = cfg.families["faq"].classes
        self.assertEqual(faq["FAQ"].index_values, [])  # no status attribute in stock iTop
        self.assertEqual(faq["FAQ"].chunks["body"].fields, ["description"])
        self.assertIsNone(cfg.families["tickets"].sweep_interval_seconds)
        self.assertIsNone(cfg.families["tickets"].log_entries_per_chunk)
