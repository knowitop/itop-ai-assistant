import unittest
from unittest.mock import AsyncMock, MagicMock

from itop_ai_assistant.config import FamilyConfig, VectorClassConfig, VectorConfig
from itop_ai_assistant.core.principal import Principal
from itop_ai_assistant.vector.sources.faq import FAMILY as FAQ_FAMILY
from itop_ai_assistant.vector.sources.registry import build_vector_sources
from itop_ai_assistant.vector.sources.tickets import FAMILY as TICKETS_FAMILY

_ENGINEER = Principal.delegated("tok", login="ivanov", name="Ivan Ivanov")


def _deps() -> MagicMock:
    deps = MagicMock()
    deps.itop = MagicMock()
    return deps


def _itop() -> MagicMock:
    """An `ItopRepos` whose two identities answer with distinguishable sets."""
    itop = MagicMock()
    service_set, principal_set = MagicMock(name="service-set"), MagicMock(name="principal-set")
    for repo_set in (service_set, principal_set):
        repo_set.ticket_repo.find_existing_ids = AsyncMock(return_value=set())
        repo_set.faq_repo.find_existing_ids = AsyncMock(return_value=set())
    itop.service = AsyncMock(return_value=service_set)
    itop.for_principal = AsyncMock(return_value=principal_set)
    return itop


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


class TestTheTwoIdentities(unittest.IsolatedAsyncioTestCase):
    """TASK-032: a source is handed one accessor per identity, and neither can
    answer as the other. Sweeping is the service account's; confirming a
    search candidate is the asker's."""

    def _tickets(self, itop: MagicMock):
        by_name = {s.name: s for s in build_vector_sources(itop, VectorConfig())}
        return by_name[TICKETS_FAMILY]

    async def test_the_sweep_reads_as_the_service_account(self):
        itop = _itop()
        source = self._tickets(itop)

        await source.prepare()

        itop.service.assert_awaited_once()
        itop.for_principal.assert_not_awaited()

    async def test_confirming_reads_as_the_principal_that_asked(self):
        itop = _itop()
        source = self._tickets(itop)

        await source.confirm_visible(_ENGINEER, "UserRequest", [1])

        itop.service.assert_not_awaited()
        itop.for_principal.assert_awaited_once()
        self.assertEqual(itop.for_principal.await_args.args[0], _ENGINEER)
        # A read never reaches the History, but `for_principal` asks for a
        # comment and the subsystem knows no run to name — a constant one is
        # what it has (TASK-032).
        self.assertIn("vector", itop.for_principal.await_args.kwargs["comment"])

    async def test_a_service_principal_takes_the_bare_connection(self):
        # `for_principal(Principal.service())` would attach a change comment
        # where there is none (`repositories/sets.py`)
        itop = _itop()
        source = self._tickets(itop)

        await source.confirm_visible(Principal.service(), "UserRequest", [1])

        itop.service.assert_awaited_once()
        itop.for_principal.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
