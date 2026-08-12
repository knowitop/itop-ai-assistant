"""The connection: its own config section, its own client, its own lifecycle.

What these tests pin down is the split itself — a mapping edit must not reach
the HTTP client, which is what it did while one fingerprint covered three
sections (TASK-027).
"""

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from itop_ai_assistant.config import (
    FaqFieldMap,
    FaqMappingConfig,
    ItopConfig,
    TicketFieldMap,
    TicketMappingConfig,
)
from itop_ai_assistant.itop_connection import ItopConnection
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


class TestClientCache(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = FakeConfigStore()
        self.connection = ItopConnection(self.store)

    async def asyncTearDown(self):
        await self.connection.aclose()

    async def test_same_config_returns_the_same_client(self):
        self.assertIs(await self.connection.client(), await self.connection.client())

    async def test_connection_change_rebuilds_and_closes_the_old_client(self):
        first = await self.connection.client()

        with patch.object(first, "aclose", new_callable=AsyncMock) as old_close:
            self.store.sections["itop"] = ItopConfig(url="http://two/rest.php", token="tok")
            second = await self.connection.client()

        self.assertIsNot(first, second)
        old_close.assert_awaited_once()

    async def test_a_mapping_edit_does_not_touch_the_client(self):
        """Before TASK-027 the fingerprint spanned three sections, so editing
        `ticket_mapping` in the admin UI closed the HTTP pool — shared with
        every principal view — to rebuild a client nothing had changed about."""
        first = await self.connection.client()

        with patch.object(first, "aclose", new_callable=AsyncMock) as old_close:
            self.store.sections["ticket_mapping"] = TicketMappingConfig(fields=TicketFieldMap(title="short_desc"))
            self.store.sections["faq_mapping"] = FaqMappingConfig(fields=FaqFieldMap(title="headline"))
            second = await self.connection.client()

        self.assertIs(first, second)
        old_close.assert_not_awaited()

    async def test_aclose_resets_the_cache(self):
        first = await self.connection.client()
        await self.connection.aclose()

        self.assertIsNot(first, await self.connection.client())


class TestAsPrincipal(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = FakeConfigStore()
        self.connection = ItopConnection(self.store)

    async def asyncTearDown(self):
        await self.connection.aclose()

    async def test_a_delegated_view_carries_the_engineers_token_and_the_comment(self):
        view = await self.connection.as_principal(_ENGINEER, comment="run 42")

        self.assertEqual(view.auth.token, "engineer-token")
        self.assertEqual(view.comment, "run 42")

    async def test_the_service_principal_keeps_the_connections_own_credentials(self):
        base = await self.connection.client()

        view = await self.connection.as_principal(Principal.service(), comment="run 42")

        self.assertEqual(view.auth, base.auth)
        self.assertEqual(view.comment, "run 42")

    async def test_the_pool_is_shared_not_duplicated(self):
        base = await self.connection.client()

        view = await self.connection.as_principal(_ENGINEER, comment="run 42")

        self.assertIsNot(view, base)
        self.assertIs(view._http, base._http)


class TestAiPersonName(unittest.IsolatedAsyncioTestCase):
    """The service account's own name — a property of the connection."""

    def setUp(self):
        self.store = FakeConfigStore()
        self.connection = ItopConnection(self.store)

    async def asyncTearDown(self):
        await self.connection.aclose()

    def _answer(self, name="ai-assistant"):
        schema = MagicMock()
        schema.find_one = AsyncMock(return_value={"friendlyname": name})
        return schema

    async def test_resolved_once_and_cached(self):
        client = await self.connection.client()
        schema = self._answer()

        with patch.object(client, "schema", return_value=schema):
            self.assertEqual(await self.connection.ai_person_name(), "ai-assistant")
            self.assertEqual(await self.connection.ai_person_name(), "ai-assistant")

        schema.find_one.assert_awaited_once()

    async def test_resolved_as_the_service_account_even_during_a_delegated_run(self):
        """The name answers "is this last comment our own" — the loop guard. It has
        to mean the service account whoever the run acts as, or an engineer's own
        comments would start reading as ours."""
        base = await self.connection.client()
        delegated = await self.connection.as_principal(_ENGINEER, comment="run 42")
        service_schema = self._answer()

        with (
            patch.object(base, "schema", return_value=service_schema) as as_service,
            patch.object(delegated, "schema") as as_engineer,
        ):
            await self.connection.ai_person_name()

        as_service.assert_called_once_with("Person")
        as_engineer.assert_not_called()

    async def test_a_service_account_without_a_person_is_an_error(self):
        client = await self.connection.client()
        schema = MagicMock()
        schema.find_one = AsyncMock(return_value=None)

        with patch.object(client, "schema", return_value=schema), self.assertRaises(ValueError):
            await self.connection.ai_person_name()

    async def test_dropped_when_the_connection_is_rebuilt(self):
        client = await self.connection.client()
        with patch.object(client, "schema", return_value=self._answer("old")):
            await self.connection.ai_person_name()

        self.store.sections["itop"] = ItopConfig(url="http://two/rest.php", token="tok")
        rebuilt = await self.connection.client()
        with patch.object(rebuilt, "schema", return_value=self._answer("new")):
            self.assertEqual(await self.connection.ai_person_name(), "new")


if __name__ == "__main__":
    unittest.main()
