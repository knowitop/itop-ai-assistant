import unittest

from graph.enrichment.nodes.utils import html_to_markdown, strip_thinking


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


if __name__ == "__main__":
    unittest.main()
