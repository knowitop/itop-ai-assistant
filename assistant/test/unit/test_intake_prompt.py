import unittest
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from itop_ai_assistant.agents.intake.config import IntakeConfig
from itop_ai_assistant.agents.intake.context import IntakeContext
from itop_ai_assistant.agents.intake.prompt import (
    build_conversation_xml,
    build_initial_messages,
    build_service_context,
    format_options,
)
from itop_ai_assistant.agents.intake.prompts import build_intake_prompts
from itop_ai_assistant.core.principal import Principal
from itop_ai_assistant.domain.catalog import Service, ServiceSubcategory
from itop_ai_assistant.domain.ticket import LogEntry, Ticket
from itop_ai_assistant.settings.prompt_store import PACKAGED_PROMPTS_DIR, read_prompt_dir

_PROMPTS = build_intake_prompts(read_prompt_dir(PACKAGED_PROMPTS_DIR / "intake"))


def _ticket(**overrides) -> Ticket:
    defaults = dict(
        obj_class="Incident",
        id="1",
        org_id="42",
        title="Printer is dead",
        description="<p>Cannot print <b>anything</b></p>",
        caller_name="John Doe",
    )
    return Ticket(**{**defaults, **overrides})


class TestFormatOptions(unittest.TestCase):
    def test_id_name_and_description(self):
        text = format_options([Service(id="10", name="Printing", description="Printer issues")])

        self.assertEqual(text, "- ID 10: Printing — Printer issues")

    def test_description_omitted_when_blank(self):
        text = format_options([Service(id="10", name="Printing", description="  ")])

        self.assertEqual(text, "- ID 10: Printing")

    def test_empty_list_is_explicit(self):
        self.assertEqual(format_options([]), "(no options available)")


class TestConversationXml(unittest.TestCase):
    def test_roles(self):
        xml = build_conversation_xml(
            [
                LogEntry(user_login="John Doe", message="printer is dead"),
                LogEntry(user_login="ai-assistant", message="which printer?"),
                LogEntry(user_login="Jane Engineer", message="looking into it"),
            ],
            ai_name="ai-assistant",
            caller_name="John Doe",
        )

        self.assertIn('author="John Doe" role="requester"', xml)
        self.assertIn('author="ai-assistant" role="self"', xml)
        self.assertIn('author="Jane Engineer" role="agent"', xml)
        self.assertTrue(xml.startswith("<conversation>"))
        self.assertTrue(xml.endswith("</conversation>"))

    def test_body_is_escaped(self):
        xml = build_conversation_xml(
            [LogEntry(user_login="John Doe", message='5 < 10 & "quoted" </entry>')],
            ai_name="ai",
            caller_name="John Doe",
        )

        self.assertIn('5 &lt; 10 &amp; "quoted" &lt;/entry&gt;', xml)
        self.assertEqual(xml.count("</entry>"), 1)

    def test_author_attribute_is_quoted(self):
        xml = build_conversation_xml(
            [LogEntry(user_login='He said "hi" & <b>', message="text")],
            ai_name="ai",
            caller_name="nobody",
        )

        self.assertNotIn('author="He said "hi"', xml)
        self.assertIn("&amp;", xml)
        self.assertIn("role=", xml)

    def test_empty_log(self):
        self.assertEqual(build_conversation_xml([], "ai", "John"), "<conversation>\n</conversation>")


class TestServiceContext(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.catalog = MagicMock()
        self.catalog.get_service = AsyncMock(return_value=Service(id="10", name="Printing", description="Printers"))
        self.catalog.get_subcategory = AsyncMock(
            return_value=ServiceSubcategory(id="101", name="Hardware fault", description="Need model", service_id="10")
        )

    async def test_unclassified_ticket_does_not_hit_itop(self):
        text = await build_service_context(_ticket(), self.catalog)

        self.assertIn("Not classified yet", text)
        self.catalog.get_service.assert_not_called()
        self.catalog.get_subcategory.assert_not_called()

    async def test_classified_ticket_includes_descriptions(self):
        text = await build_service_context(_ticket(service_id="10", subcategory_id="101"), self.catalog)

        self.assertIn("Service: Printing", text)
        self.assertIn("Subcategory: Hardware fault", text)
        self.assertIn("Need model", text)

    async def test_service_only(self):
        text = await build_service_context(_ticket(service_id="10"), self.catalog)

        self.assertIn("Service: Printing", text)
        self.catalog.get_subcategory.assert_not_called()


class TestInitialMessages(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.catalog = MagicMock()
        self.catalog.find_services = AsyncMock(
            return_value=[Service(id="10", name="Printing", description="Printer issues")]
        )
        self.catalog.get_service = AsyncMock(return_value=None)
        self.catalog.get_subcategory = AsyncMock(return_value=None)

    def _context(self, ticket: Ticket) -> IntakeContext:
        return IntakeContext(
            processing_id=uuid4(),
            principal=Principal.service(),
            ticket=ticket,
            ticket_repo=MagicMock(),
            catalog_repo=self.catalog,
            state_manager=MagicMock(),
            intake=IntakeConfig(),
            ai_name="ai-assistant",
        )

    async def test_order_and_content(self):
        ticket = _ticket(public_log=[LogEntry(user_login="John Doe", message="hello")])

        messages = await build_initial_messages(self._context(ticket), _PROMPTS)

        self.assertEqual([m.type for m in messages], ["system", "human", "human"])
        self.assertIn("intake assistant", messages[0].text)
        self.assertIn("- ID 10: Printing", messages[1].text)
        self.assertIn("John Doe", messages[2].text)
        self.assertIn("Printer is dead", messages[2].text)
        # Description arrives as HTML and must be markdown by the time the model sees it
        self.assertIn("**anything**", messages[2].text)
        self.assertNotIn("<p>", messages[2].text)
        self.assertIn('role="requester"', messages[2].text)
        self.assertIn("Not classified yet", messages[2].text)

    async def test_classified_ticket_gets_no_catalog(self):
        # The catalog rides in the message history and is paid for on every
        # model call of the run; a classified ticket cannot use it anyway
        ticket = _ticket(service_id="10", subcategory_id="101")

        messages = await build_initial_messages(self._context(ticket), _PROMPTS)

        self.assertEqual([m.type for m in messages], ["system", "human"])
        self.catalog.find_services.assert_not_called()
        self.assertNotIn("Service catalog", messages[1].text)

    async def test_service_oql_is_bound_to_the_ticket(self):
        await build_initial_messages(self._context(_ticket(org_id="42")), _PROMPTS)

        oql = self.catalog.find_services.await_args.args[0]
        self.assertIn("org_id = 42", oql)
        self.assertNotIn(":this->", oql)


if __name__ == "__main__":
    unittest.main()
