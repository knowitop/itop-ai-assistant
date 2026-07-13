import unittest

from domain.ticket import LogEntry
from vector.chunker import CHARS_PER_TOKEN, Chunk, chunk_object, clean_text, split_text

_PROFILE = {
    "profile": ["title", "service", "subcategory"],
    "body": ["description"],
    "solution": ["solution"],
}


def _chunk(
    fields: dict[str, str], profile=None, *, max_chunk_tokens=100, log_entries_per_chunk=5, **kwargs
) -> list[Chunk]:
    return chunk_object(
        fields,
        profile or _PROFILE,
        max_chunk_tokens=max_chunk_tokens,
        log_entries_per_chunk=log_entries_per_chunk,
        **kwargs,
    )


class TestFieldChunks(unittest.TestCase):
    def test_profile_keys_become_chunk_kinds(self):
        chunks = _chunk(
            {
                "title": "Printer broken",
                "service": "Printing",
                "subcategory": "Hardware",
                "description": "<p>Not printing.</p>",
                "solution": "Replaced the cartridge.",
            }
        )

        by_kind = {c.kind: c for c in chunks}
        self.assertEqual(set(by_kind), {"profile", "body", "solution"})
        self.assertEqual(by_kind["profile"].text, "Printer broken\n\nPrinting\n\nHardware")
        self.assertEqual(by_kind["body"].text, "Not printing.")
        self.assertTrue(all(c.visibility == "public" for c in chunks))
        self.assertTrue(all(c.n == 0 for c in chunks))

    def test_empty_solution_yields_no_chunk(self):
        chunks = _chunk({"title": "T", "service": "", "subcategory": "", "description": "D", "solution": ""})

        self.assertNotIn("solution", {c.kind for c in chunks})

    def test_all_empty_yields_nothing(self):
        self.assertEqual(_chunk({k: "" for k in ("title", "service", "subcategory", "description", "solution")}), [])

    def test_hash_stable_under_cosmetic_html(self):
        fields = {"title": "", "service": "", "subcategory": "", "solution": ""}
        plain = _chunk({**fields, "description": "Hello world"})
        html = _chunk({**fields, "description": "<p>Hello   world</p>"})

        self.assertEqual(plain[0].content_hash, html[0].content_hash)

    def test_hash_changes_with_content(self):
        fields = {"title": "", "service": "", "subcategory": "", "solution": ""}
        a = _chunk({**fields, "description": "Hello world"})
        b = _chunk({**fields, "description": "Hello there"})

        self.assertNotEqual(a[0].content_hash, b[0].content_hash)

    def test_unknown_field_in_profile_treated_as_empty(self):
        with self.assertLogs("vector.chunker", level="WARNING"):
            chunks = _chunk({"description": "Text"}, {"body": ["description", "no_such_field"]})

        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0].text, "Text")


class TestSplitText(unittest.TestCase):
    def test_short_text_single_piece(self):
        self.assertEqual(split_text("hello", 100), ["hello"])
        self.assertEqual(split_text("", 100), [])

    def test_greedy_paragraph_packing(self):
        text = "aaaa\n\nbbbb\n\ncccc"
        pieces = split_text(text, 11)

        self.assertEqual(pieces, ["aaaa\n\nbbbb", "cccc"])

    def test_oversize_paragraph_splits_on_sentences(self):
        text = "First sentence here. Second sentence here. Third sentence here."
        pieces = split_text(text, 45)

        self.assertTrue(all(len(p) <= 45 for p in pieces))
        self.assertEqual(len(pieces), 2)
        self.assertIn("First sentence here.", pieces[0])

    def test_oversize_sentence_hard_sliced(self):
        text = "x" * 25
        pieces = split_text(text, 10)

        self.assertEqual(pieces, ["x" * 10, "x" * 10, "x" * 5])

    def test_deterministic(self):
        text = ("Sentence one. Sentence two. " * 20 + "\n\n") * 3
        self.assertEqual(split_text(text, 100), split_text(text, 100))

    def test_multi_chunk_ordinals(self):
        budget_tokens = 4  # 12 chars
        chunks = _chunk(
            {"title": "", "service": "", "subcategory": "", "solution": "", "description": "aaaa\n\nbbbb\n\ncccc"},
            max_chunk_tokens=budget_tokens,
        )

        self.assertEqual([(c.kind, c.n) for c in chunks], [("body", 0), ("body", 1)])
        self.assertEqual(budget_tokens * CHARS_PER_TOKEN, 12)


class TestLogChunks(unittest.TestCase):
    _PROFILE: dict = {"log:public": [], "log:private": []}

    @staticmethod
    def _entries(n: int, login: str = "John Doe") -> list[LogEntry]:
        return [LogEntry(user_login=login, message=f"message {i}") for i in range(n)]

    def _log_chunks(self, logs, **kwargs) -> list[Chunk]:
        return _chunk({}, self._PROFILE, logs=logs, **kwargs)

    def test_window_boundaries_by_entry_index(self):
        chunks = self._log_chunks({"log:public": self._entries(7)}, log_entries_per_chunk=5)

        public = [c for c in chunks if c.kind == "log:public"]
        self.assertEqual([c.n for c in public], [0, 1])
        self.assertEqual(public[0].text.count("\n") + 1, 5)
        self.assertEqual(public[1].text.count("\n") + 1, 2)

    def test_appending_entries_only_changes_last_chunk(self):
        before = self._log_chunks({"log:public": self._entries(7)}, log_entries_per_chunk=5)
        after = self._log_chunks({"log:public": self._entries(8)}, log_entries_per_chunk=5)

        self.assertEqual(before[0].content_hash, after[0].content_hash)
        self.assertEqual(before[0].text, after[0].text)  # byte-for-byte
        self.assertNotEqual(before[1].content_hash, after[1].content_hash)

    def test_role_prefixes(self):
        entries = [
            LogEntry(user_login="John Doe", message="I have a problem"),
            LogEntry(user_login="Jane Agent", message="Looking into it"),
        ]
        chunks = self._log_chunks({"log:public": entries}, caller_name="John Doe")

        text = chunks[0].text
        self.assertIn("caller: I have a problem", text)
        self.assertIn("agent: Looking into it", text)

    def test_private_log_is_internal(self):
        chunks = self._log_chunks({"log:private": self._entries(1), "log:public": self._entries(1)})

        by_kind = {c.kind: c.visibility for c in chunks}
        self.assertEqual(by_kind["log:private"], "internal")
        self.assertEqual(by_kind["log:public"], "public")

    def test_entries_truncated_to_share_of_budget(self):
        # budget = 10 tokens * 3 = 30 chars; per entry = 30 // 5 = 6 chars
        entries = [LogEntry(user_login="x", message="a" * 100)]
        chunks = self._log_chunks({"log:public": entries}, max_chunk_tokens=10, log_entries_per_chunk=5)

        self.assertEqual(chunks[0].text, "agent: " + "a" * 6)

    def test_windows_never_resplit(self):
        # Oversize window is truncated per entry, never split into more chunks
        entries = [LogEntry(user_login="x", message="b" * 500) for _ in range(5)]
        chunks = self._log_chunks({"log:public": entries}, max_chunk_tokens=10, log_entries_per_chunk=5)

        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0].n, 0)


class TestCleanText(unittest.TestCase):
    def test_strips_html_and_collapses_whitespace(self):
        self.assertEqual(clean_text("<p>Hello   <b>world</b></p>"), "Hello **world**")
        self.assertEqual(clean_text("<p>a</p><p>b</p>"), "a\n\nb")
        self.assertEqual(clean_text(None), "")


if __name__ == "__main__":
    unittest.main()
