import unittest

from itop_ai_assistant.vector.chunker import (
    CHARS_PER_TOKEN,
    Chunk,
    FragmentContent,
    SequenceContent,
    TextContent,
    chunk_object,
    clean_text,
    split_text,
)


def _text(kind: str, text: str, visibility: str = "public") -> FragmentContent:
    return FragmentContent(kind=kind, visibility=visibility, content=TextContent(text))


def _sequence(kind: str, items: list[str], visibility: str = "public") -> FragmentContent:
    return FragmentContent(kind=kind, visibility=visibility, content=SequenceContent(items))


def _chunk(fragments: list[FragmentContent], *, max_chunk_tokens=100, items_per_window=5) -> list[Chunk]:
    return chunk_object(fragments, max_chunk_tokens=max_chunk_tokens, items_per_window=items_per_window)


class TestTextFragments(unittest.TestCase):
    def test_fragment_kind_and_order_preserved(self):
        chunks = _chunk(
            [
                _text("profile", "Printer broken\n\nPrinting\n\nHardware"),
                _text("body", "Not printing."),
                _text("solution", "Replaced the cartridge."),
            ]
        )

        self.assertEqual([c.kind for c in chunks], ["profile", "body", "solution"])
        self.assertEqual(chunks[0].text, "Printer broken\n\nPrinting\n\nHardware")
        self.assertTrue(all(c.n == 0 for c in chunks))

    def test_empty_content_yields_no_chunk(self):
        chunks = _chunk([_text("body", "D"), _text("solution", "")])

        self.assertEqual([c.kind for c in chunks], ["body"])

    def test_nothing_at_all_yields_nothing(self):
        self.assertEqual(_chunk([]), [])

    def test_hash_follows_content(self):
        a = _chunk([_text("body", "Hello world")])
        b = _chunk([_text("body", "Hello world")])
        c = _chunk([_text("body", "Hello there")])

        self.assertEqual(a[0].content_hash, b[0].content_hash)
        self.assertNotEqual(a[0].content_hash, c[0].content_hash)

    def test_multi_chunk_ordinals(self):
        budget_tokens = 4  # 12 chars
        chunks = _chunk([_text("body", "aaaa\n\nbbbb\n\ncccc")], max_chunk_tokens=budget_tokens)

        self.assertEqual([(c.kind, c.n) for c in chunks], [("body", 0), ("body", 1)])
        self.assertEqual(budget_tokens * CHARS_PER_TOKEN, 12)


class TestVisibility(unittest.TestCase):
    """Visibility travels with the content the source hands over — the
    chunker has no rule of its own to derive it from (ADR-018), which is what
    keeps it out of reach of the config (backlog B6)."""

    def test_taken_from_the_fragment_verbatim(self):
        chunks = _chunk(
            [
                _text("body", "public text"),
                _text("notes", "internal text", visibility="internal"),
                _sequence("log:private", ["agent: note"], visibility="internal"),
            ]
        )

        self.assertEqual({c.kind: c.visibility for c in chunks}["body"], "public")
        self.assertEqual({c.kind: c.visibility for c in chunks}["notes"], "internal")
        self.assertEqual({c.kind: c.visibility for c in chunks}["log:private"], "internal")

    def test_kind_name_carries_no_meaning(self):
        # "log:private" used to imply internal by name alone; it no longer does
        chunks = _chunk([_sequence("log:private", ["agent: note"], visibility="public")])

        self.assertEqual(chunks[0].visibility, "public")


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


class TestSequenceFragments(unittest.TestCase):
    @staticmethod
    def _items(n: int, speaker: str = "agent") -> list[str]:
        return [f"{speaker}: message {i}" for i in range(n)]

    def test_window_boundaries_by_item_index(self):
        chunks = _chunk([_sequence("log:public", self._items(7))], items_per_window=5)

        self.assertEqual([c.n for c in chunks], [0, 1])
        self.assertEqual(chunks[0].text.count("\n") + 1, 5)
        self.assertEqual(chunks[1].text.count("\n") + 1, 2)

    def test_appending_items_only_changes_last_chunk(self):
        before = _chunk([_sequence("log:public", self._items(7))], items_per_window=5)
        after = _chunk([_sequence("log:public", self._items(8))], items_per_window=5)

        self.assertEqual(before[0].content_hash, after[0].content_hash)
        self.assertEqual(before[0].text, after[0].text)  # byte-for-byte
        self.assertNotEqual(before[1].content_hash, after[1].content_hash)

    def test_items_pass_through_verbatim(self):
        # Labelling ("who said it") is the source's job — the chunker packs
        # the strings it is given and adds nothing.
        chunks = _chunk([_sequence("log:public", ["caller: I have a problem", "agent: Looking into it"])])

        self.assertEqual(chunks[0].text, "caller: I have a problem\nagent: Looking into it")

    def test_empty_sequence_yields_no_chunk(self):
        self.assertEqual(_chunk([_sequence("log:public", [])]), [])

    def test_items_truncated_to_share_of_budget(self):
        # budget = 10 tokens * 3 = 30 chars; per item = 30 // 5 = 6 chars
        chunks = _chunk([_sequence("log:public", ["a" * 100])], max_chunk_tokens=10, items_per_window=5)

        self.assertEqual(chunks[0].text, "a" * 6)

    def test_windows_never_resplit(self):
        # An oversize window is truncated per item, never split into more chunks
        chunks = _chunk([_sequence("log:public", ["b" * 500] * 5)], max_chunk_tokens=10, items_per_window=5)

        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0].n, 0)


class TestCleanText(unittest.TestCase):
    def test_strips_html_and_collapses_whitespace(self):
        self.assertEqual(clean_text("<p>Hello   <b>world</b></p>"), "Hello **world**")
        self.assertEqual(clean_text("<p>a</p><p>b</p>"), "a\n\nb")
        self.assertEqual(clean_text(None), "")

    def test_not_idempotent_so_callers_must_apply_it_once(self):
        # Why canonicalization belongs to the source and not to chunk_object:
        # markdownify escapes markdown syntax, so a second pass mangles text.
        once = clean_text("<p>Hello <b>world</b></p>")
        self.assertNotEqual(clean_text(once), once)


if __name__ == "__main__":
    unittest.main()
