import unittest
from unittest.mock import AsyncMock, patch

from config import ItopConfig, LlmConfig, TicketMappingConfig
from deps import ItopProvider, create_llm


class _FakeConfigStore:
    def __init__(self):
        self.sections = {
            "itop": ItopConfig(url="http://one/rest.php", token="tok"),
            "ticket_mapping": TicketMappingConfig(),
        }

    async def get(self, module, model):
        return self.sections[module]


class TestItopProvider(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = _FakeConfigStore()
        self.provider = ItopProvider(self.store)

    async def test_same_config_returns_same_bundle(self):
        b1 = await self.provider.get()
        b2 = await self.provider.get()
        self.assertIs(b1, b2)
        self.assertIs(b1.ticket_repo, b2.ticket_repo)
        await self.provider.aclose()

    async def test_config_change_rebuilds_and_closes_old_client(self):
        b1 = await self.provider.get()
        with patch.object(b1.client, "aclose", new_callable=AsyncMock) as old_close:
            self.store.sections["itop"] = ItopConfig(url="http://two/rest.php", token="tok")
            b2 = await self.provider.get()

        self.assertIsNot(b1, b2)
        old_close.assert_awaited_once()
        await self.provider.aclose()

    async def test_mapping_change_rebuilds_repositories(self):
        b1 = await self.provider.get()
        self.store.sections["ticket_mapping"] = TicketMappingConfig(active_statuses=["new", "assigned"])

        b2 = await self.provider.get()

        self.assertIsNot(b1.ticket_repo, b2.ticket_repo)
        self.assertEqual(b2.ticket_repo.mapping.active_statuses, ["new", "assigned"])
        await self.provider.aclose()

    async def test_aclose_resets_cache(self):
        b1 = await self.provider.get()
        await self.provider.aclose()
        b2 = await self.provider.get()
        self.assertIsNot(b1, b2)
        await self.provider.aclose()


class TestCreateLlm(unittest.TestCase):
    def test_model_override(self):
        llm = create_llm(LlmConfig(model="default-model", api_key="k"), model="special")
        self.assertEqual(llm.model_name, "special")

    def test_defaults_from_config(self):
        llm = create_llm(LlmConfig(base_url="http://llm/v1", model="default-model", api_key="k"))
        self.assertEqual(llm.model_name, "default-model")
        self.assertEqual(llm.openai_api_base, "http://llm/v1")

    def test_missing_api_key_gets_placeholder(self):
        # Local endpoints (LM Studio) ignore the key, but ChatOpenAI needs one
        llm = create_llm(LlmConfig(model="m"))
        self.assertIsNotNone(llm.openai_api_key)


if __name__ == "__main__":
    unittest.main()
