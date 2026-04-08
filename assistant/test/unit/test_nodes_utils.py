import unittest

from graph.enrichment.nodes.utils import strip_thinking


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


if __name__ == "__main__":
    unittest.main()
