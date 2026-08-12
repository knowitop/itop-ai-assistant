import unittest
from unittest.mock import MagicMock

from itop_ai_assistant.config import FamilyConfig, VectorClassConfig, VectorConfig
from itop_ai_assistant.vector.sources.faq import FAMILY as FAQ_FAMILY
from itop_ai_assistant.vector.sources.registry import build_vector_sources
from itop_ai_assistant.vector.sources.tickets import FAMILY as TICKETS_FAMILY


def _deps() -> MagicMock:
    deps = MagicMock()
    deps.itop = MagicMock()
    return deps


class TestBuildVectorSources(unittest.TestCase):
    """TASK-021: every registered family is built unconditionally — the admin
    UI's vocabulary (`GET /api/vector/sources`) must not depend on what the
    saved config still happens to contain, or a family emptied by mistake
    becomes unrecoverable from the UI."""

    def test_every_registered_family_is_built_by_default(self):
        sources = build_vector_sources(_deps(), VectorConfig())

        self.assertEqual({s.name for s in sources}, {TICKETS_FAMILY, FAQ_FAMILY})

    def test_a_family_missing_from_config_entirely_is_still_built_empty(self):
        cfg = VectorConfig(families={"tickets": FamilyConfig(classes={"UserRequest": VectorClassConfig()})})

        sources = build_vector_sources(_deps(), cfg)

        by_name = {s.name: s for s in sources}
        self.assertEqual(set(by_name), {TICKETS_FAMILY, FAQ_FAMILY})
        self.assertEqual(list(by_name[FAQ_FAMILY].classes), [])

    def test_classes_come_from_the_matching_family_only(self):
        cfg = VectorConfig(
            families={
                "tickets": FamilyConfig(classes={"Problem": VectorClassConfig()}),
                "faq": FamilyConfig(classes={}),
            }
        )

        sources = build_vector_sources(_deps(), cfg)

        by_name = {s.name: s for s in sources}
        self.assertEqual(list(by_name[TICKETS_FAMILY].classes), ["Problem"])
        self.assertEqual(list(by_name[FAQ_FAMILY].classes), [])

    def test_an_unrecognized_family_key_is_ignored_not_rejected(self):
        cfg = VectorConfig(
            families={"kb_articles": FamilyConfig(classes={"KnowledgeBaseArticle": VectorClassConfig()})}
        )

        sources = build_vector_sources(_deps(), cfg)

        # Both known families are still built; the unrecognized one is
        # dropped with a warning, not a crash — same tolerance as an
        # unrecognized class within a known family.
        self.assertEqual({s.name for s in sources}, {TICKETS_FAMILY, FAQ_FAMILY})


if __name__ == "__main__":
    unittest.main()
