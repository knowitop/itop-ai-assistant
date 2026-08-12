import unittest
from unittest.mock import AsyncMock, MagicMock

from itop_ai_assistant.identity_repository import IdentityRepository


class TestIdentityRepository(unittest.IsolatedAsyncioTestCase):
    def _itop(self, person: dict | None):
        schema = MagicMock()
        schema.find_one = AsyncMock(return_value=person)
        itop = MagicMock()
        itop.schema = MagicMock(return_value=schema)
        return itop, schema

    async def test_reads_the_person_linked_to_the_current_account(self):
        itop, schema = self._itop({"friendlyname": "AI Assistant"})

        name = await IdentityRepository(itop).current_person_name()

        self.assertEqual(name, "AI Assistant")
        itop.schema.assert_called_once_with("Person")
        schema.find_one.assert_awaited_once_with({"id": ("=", ":current_contact_id")}, projection=["friendlyname"])

    async def test_no_person_linked_is_an_error_not_an_empty_name(self):
        """The setup wizard probes for exactly this state — an unlinked service
        account must fail loudly, not hand the loop guard an empty name."""
        itop, _ = self._itop(None)

        with self.assertRaises(ValueError):
            await IdentityRepository(itop).current_person_name()


if __name__ == "__main__":
    unittest.main()
