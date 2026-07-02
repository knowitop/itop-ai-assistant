import unittest

from langchain_core.messages import AIMessage, HumanMessage

from graph.enrichment.nodes.utils import bind_oql, build_conversation, html_to_markdown, strip_thinking


class TestBindOql(unittest.TestCase):
    def test_numeric_value_substituted_bare(self):
        self.assertEqual(
            bind_oql("SELECT Service WHERE org_id = :this->org_id", {"org_id": "42"}),
            "SELECT Service WHERE org_id = 42",
        )

    def test_none_becomes_null(self):
        self.assertEqual(
            bind_oql("SELECT S WHERE ISNULL(:this->request_type)", {"request_type": None}),
            "SELECT S WHERE ISNULL(NULL)",
        )

    def test_string_value_quoted(self):
        self.assertEqual(
            bind_oql("SELECT S WHERE request_type = :this->request_type", {"request_type": "incident"}),
            'SELECT S WHERE request_type = "incident"',
        )

    def test_quotes_in_value_escaped(self):
        result = bind_oql("SELECT S WHERE name = :this->name", {"name": 'a" OR 1=1 --'})
        self.assertEqual(result, 'SELECT S WHERE name = "a\\" OR 1=1 --"')

    def test_backslash_in_value_escaped(self):
        result = bind_oql("SELECT S WHERE name = :this->name", {"name": "dom\\user"})
        self.assertEqual(result, 'SELECT S WHERE name = "dom\\\\user"')

    def test_key_prefix_does_not_clobber_longer_key(self):
        result = bind_oql("SELECT S WHERE a = :this->org AND b = :this->org_id", {"org": "7", "org_id": "42"})
        self.assertEqual(result, "SELECT S WHERE a = 7 AND b = 42")

    def test_keys_without_placeholder_ignored(self):
        oql = "SELECT Service WHERE org_id = :this->org_id"
        result = bind_oql(oql, {"org_id": "1", "public_log": {"entries": []}})
        self.assertEqual(result, "SELECT Service WHERE org_id = 1")

    def test_int_value_accepted(self):
        self.assertEqual(
            bind_oql("SELECT S WHERE service_id = :this->service_id", {"service_id": 5}),
            "SELECT S WHERE service_id = 5",
        )


class TestStripThinking(unittest.TestCase):
    def test_none_returns_empty(self):
        self.assertEqual(strip_thinking(None), "")

    def test_empty_string_returns_empty(self):
        self.assertEqual(strip_thinking(""), "")

    def test_whitespace_only_returns_empty(self):
        self.assertEqual(strip_thinking("   "), "")

    def test_plain_text_unchanged(self):
        self.assertEqual(strip_thinking("hello"), "hello")

    def test_think_block_stripped(self):
        self.assertEqual(strip_thinking("<think>reasoning</think>answer"), "answer")

    def test_only_think_block_returns_empty(self):
        self.assertEqual(strip_thinking("<think>reasoning</think>"), "")

    def test_multiline_think_block_stripped(self):
        self.assertEqual(strip_thinking("<think>\nline1\nline2\n</think>result"), "result")


class TestHtmlToMarkdown(unittest.TestCase):
    def test_none_returns_empty(self):
        self.assertEqual(html_to_markdown(None), "")

    def test_empty_string_returns_empty(self):
        self.assertEqual(html_to_markdown(""), "")

    def test_plain_text_unchanged(self):
        self.assertEqual(html_to_markdown("hello"), "hello")

    def test_paragraph_tag_stripped(self):
        self.assertEqual(html_to_markdown("<p>hello</p>"), "hello")

    def test_bold_converted_to_markdown(self):
        self.assertEqual(html_to_markdown("<strong>important</strong>"), "**important**")

    def test_italic_converted_to_markdown(self):
        self.assertEqual(html_to_markdown("<em>note</em>"), "*note*")

    def test_unordered_list_converted(self):
        result = html_to_markdown("<ul><li>one</li><li>two</li></ul>")
        self.assertIn("one", result)
        self.assertIn("two", result)
        self.assertIn("*", result)

    def test_table_structure_preserved(self):
        result = html_to_markdown("<table><tr><td>Server</td><td>db-01</td></tr></table>")
        self.assertIn("Server", result)
        self.assertIn("db-01", result)
        self.assertIn("|", result)

    def test_html_entities_decoded(self):
        self.assertEqual(html_to_markdown("&lt;MyClass&gt;"), "<MyClass>")

    def test_amp_entity_decoded(self):
        self.assertEqual(html_to_markdown("cats &amp; dogs"), "cats & dogs")

    def test_script_tag_stripped(self):
        result = html_to_markdown("<p>text</p><script>alert(1)</script>")
        self.assertNotIn("alert", result)
        self.assertIn("text", result)

    def test_style_tag_stripped(self):
        result = html_to_markdown("<p>text</p><style>.foo{color:red}</style>")
        self.assertNotIn("color", result)
        self.assertIn("text", result)

    def test_nested_tags(self):
        result = html_to_markdown("<p>Hello <strong>world</strong></p>")
        self.assertIn("Hello", result)
        self.assertIn("**world**", result)


class TestBuildConversation(unittest.TestCase):
    def test_empty_entries_returns_empty_list(self):
        result = build_conversation([], "ai-assistant", "John Doe")
        self.assertEqual(result, [])

    def test_ai_entry_becomes_ai_message(self):
        entries = [{"user_login": "ai-assistant", "message": "What is your laptop model?"}]
        result = build_conversation(entries, "ai-assistant", "John Doe")
        self.assertEqual(len(result), 1)
        self.assertIsInstance(result[0], AIMessage)
        self.assertEqual(result[0].content, "What is your laptop model?")

    def test_caller_entry_becomes_human_message_with_requester_label(self):
        entries = [{"user_login": "John Doe", "message": "It does not turn on."}]
        result = build_conversation(entries, "ai-assistant", "John Doe")
        self.assertEqual(len(result), 1)
        self.assertIsInstance(result[0], HumanMessage)
        self.assertIn("[Requester]", result[0].content)
        self.assertIn("It does not turn on.", result[0].content)

    def test_third_party_entry_has_no_requester_label(self):
        entries = [{"user_login": "engineer", "message": "Checked the device."}]
        result = build_conversation(entries, "ai-assistant", "John Doe")
        self.assertEqual(len(result), 1)
        self.assertIsInstance(result[0], HumanMessage)
        self.assertNotIn("[Requester]", result[0].content)
        self.assertIn("engineer", result[0].content)
        self.assertIn("Checked the device.", result[0].content)

    def test_mixed_entries_preserve_order(self):
        entries = [
            {"user_login": "John Doe", "message": "Help!"},
            {"user_login": "ai-assistant", "message": "What model?"},
            {"user_login": "John Doe", "message": "Dell XPS 13."},
        ]
        result = build_conversation(entries, "ai-assistant", "John Doe")
        self.assertEqual(len(result), 3)
        self.assertIsInstance(result[0], HumanMessage)
        self.assertIsInstance(result[1], AIMessage)
        self.assertIsInstance(result[2], HumanMessage)

    def test_human_message_name_set_to_user_login(self):
        entries = [{"user_login": "John Doe", "message": "Hello."}]
        result = build_conversation(entries, "ai-assistant", "John Doe")
        self.assertEqual(result[0].name, "John Doe")


if __name__ == "__main__":
    unittest.main()
