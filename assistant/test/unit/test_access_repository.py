import unittest
from unittest.mock import AsyncMock, MagicMock

from itop_ai_assistant.repositories.access import AccessRepository


def _make_repo(response: dict | None) -> tuple[AccessRepository, MagicMock, MagicMock]:
    schema = MagicMock()
    schema.find_one = AsyncMock(return_value=response)
    itop = MagicMock()
    itop.schema = MagicMock(return_value=schema)
    return AccessRepository(itop), itop, schema


class TestAllowedOrgIds(unittest.IsolatedAsyncioTestCase):
    async def test_queries_user_by_current_contact(self):
        repo, itop, schema = _make_repo({"allowed_org_list": [{"allowed_org_id": "3"}]})

        await repo.allowed_org_ids()

        itop.schema.assert_called_once_with("User")
        schema.find_one.assert_awaited_once_with(
            {"contactid": ("=", ":current_contact_id")}, projection=["allowed_org_list"]
        )

    async def test_returns_org_ids_as_strings(self):
        repo, _, _ = _make_repo({"allowed_org_list": [{"allowed_org_id": "3"}, {"allowed_org_id": 7}]})

        self.assertEqual(await repo.allowed_org_ids(), ["3", "7"])

    async def test_empty_list_means_every_organization_is_allowed(self):
        repo, _, _ = _make_repo({"allowed_org_list": []})

        self.assertIsNone(await repo.allowed_org_ids())

    async def test_missing_field_means_unrestricted(self):
        repo, _, _ = _make_repo({})

        self.assertIsNone(await repo.allowed_org_ids())

    async def test_no_matching_user_means_unrestricted(self):
        repo, _, _ = _make_repo(None)

        self.assertIsNone(await repo.allowed_org_ids())


if __name__ == "__main__":
    unittest.main()
