"""The repository layer: one set per principal, built in exactly one place.

The mapping sections belong here and not to the connection — a repository is
what they configure. What these tests hold is the pair of invariants the old
`ItopBundle` carried: a set never mixes principals, and nothing in it hands out
the client (TASK-027).
"""

import unittest

from itop_ai_assistant.config import (
    FaqFieldMap,
    FaqMappingConfig,
    ItopConfig,
    TicketFieldMap,
    TicketMappingConfig,
)
from itop_ai_assistant.itop_connection import ItopConnection, ItopRepositories, RepositorySet
from itop_ai_assistant.principal import Principal

_ENGINEER = Principal.delegated("engineer-token", login="jdoe", name="John Doe")


class FakeConfigStore:
    def __init__(self):
        self.sections = {
            "itop": ItopConfig(url="http://one/rest.php", token="tok"),
            "ticket_mapping": TicketMappingConfig(),
            "faq_mapping": FaqMappingConfig(),
        }

    async def get(self, module, model):
        return self.sections[module]


class TestRepositorySet(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = FakeConfigStore()
        self.connection = ItopConnection(self.store)
        self.repositories = ItopRepositories(self.connection, self.store)

    async def asyncTearDown(self):
        await self.connection.aclose()

    async def test_every_repository_in_a_set_talks_as_the_same_principal(self):
        repos = await self.repositories.for_principal(_ENGINEER, comment="run 42")

        client = repos.ticket_repo._itop
        self.assertIs(repos.catalog_repo._itop, client)
        self.assertIs(repos.access_repo._itop, client)
        self.assertIs(repos.faq_repo._itop, client)
        self.assertEqual(client.auth.token, "engineer-token")
        self.assertEqual(client.comment, "run 42")

    async def test_the_set_hands_out_no_client(self):
        """`ItopBundle.client` was public and unused outside the provider —
        an open door nobody walked through, against the rule that nothing
        outside the repositories touches iTop."""
        repos = await self.repositories.service()

        self.assertFalse(hasattr(repos, "client"))

    async def test_identity_is_not_reachable_from_a_run(self):
        """`current_person_name()` off a delegated set would resolve the
        engineer, and the intake loop guard compares that name against the
        author of the last public comment."""
        repos = await self.repositories.for_principal(_ENGINEER, comment="run 42")

        self.assertFalse(hasattr(repos, "identity_repo"))

    async def test_the_service_set_runs_on_the_bare_connection(self):
        repos = await self.repositories.service()

        self.assertIs(repos.ticket_repo._itop, await self.connection.client())

    async def test_the_pool_is_shared_with_the_connection(self):
        base = await self.connection.client()

        repos = await self.repositories.for_principal(_ENGINEER, comment="run 42")

        self.assertIs(repos.ticket_repo._itop._http, base._http)

    async def test_the_mapping_of_the_moment_reaches_the_repositories(self):
        self.store.sections["ticket_mapping"] = TicketMappingConfig(fields=TicketFieldMap(title="short_desc"))
        self.store.sections["faq_mapping"] = FaqMappingConfig(fields=FaqFieldMap(title="headline"))

        repos = await self.repositories.service()

        self.assertEqual(repos.ticket_repo.mapping.fields.title, "short_desc")
        self.assertEqual(repos.faq_repo.mapping.fields.title, "headline")

    async def test_a_mapping_edit_applies_to_the_next_set_without_a_reconnect(self):
        first = await self.repositories.service()
        client = await self.connection.client()

        self.store.sections["ticket_mapping"] = TicketMappingConfig(fields=TicketFieldMap(title="short_desc"))
        second = await self.repositories.service()

        self.assertEqual(first.ticket_repo.mapping.fields.title, "title")
        self.assertEqual(second.ticket_repo.mapping.fields.title, "short_desc")
        self.assertIs(await self.connection.client(), client)

    async def test_ai_person_name_is_answered_by_the_connection(self):
        self.assertTrue(hasattr(self.repositories, "ai_person_name"))

    async def test_the_set_is_a_record_not_a_factory(self):
        repos = await self.repositories.service()

        self.assertIsInstance(repos, RepositorySet)
        self.assertEqual(
            {f for f in RepositorySet.__dataclass_fields__},
            {"ticket_repo", "catalog_repo", "access_repo", "faq_repo"},
        )


if __name__ == "__main__":
    unittest.main()
