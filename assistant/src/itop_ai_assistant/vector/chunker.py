"""Content → chunks: pure, deterministic functions (no I/O), source-agnostic.

Determinism matters: the sweep's hash-guard compares sha256 of the *final*
chunk text against what is stored, so the same input must always produce
byte-identical chunks — the split algorithm has no randomness, and the text
it receives is expected to be canonical already. Canonicalizing is the
source's job (`clean_text` lives here and is exported for exactly that):
whether a field holds HTML, markdown or plain text is domain knowledge, and
`clean_text` is not idempotent, so it must run exactly once.

This module knows nothing about chunk kinds, semantic fields, logs or
visibility rules: the source resolves its own config into `FragmentContent`s
and hands them over already labeled (ADR-018). What is left here is packing,
in two strategies, and the source picks one by the shape of the content it
passes:

- `TextContent` — greedy paragraph packing, the whole text re-packed on every
  change;
- `SequenceContent` — windows of N items with boundaries fixed by item index,
  so appending an item only rewrites the last window and the earlier chunks'
  hashes never move (no re-embedding of old history).
"""

import hashlib
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass

from itop_ai_assistant.util.text import html_to_markdown

logger = logging.getLogger(__name__)

# Conservative chars-per-token estimate for mixed ru/en text. The budget
# guards the embedding model's context window, so erring low is safe: chunks
# get smaller, never truncated.
CHARS_PER_TOKEN = 3

_SENTENCE_RE = re.compile(r"(?<=[.!?…])\s+")
_SPACES_RE = re.compile(r"[ \t]+")
_BLANK_LINES_RE = re.compile(r"\n{3,}")


@dataclass(frozen=True)
class Chunk:
    kind: str
    n: int
    text: str
    visibility: str  # public / internal
    content_hash: str


@dataclass(frozen=True)
class TextContent:
    """Free canonical text — packed by paragraphs."""

    text: str


@dataclass(frozen=True)
class SequenceContent:
    """An ordered, append-only sequence (a conversation log, a list of
    revisions) — packed by index-aligned windows. Items are canonical
    single-line-or-more strings rendered by the source: it owns the "who said
    it" part, this module does not."""

    items: Sequence[str]


@dataclass(frozen=True)
class FragmentContent:
    """One fragment of an object, resolved by its source: what it is called,
    who may see it, and the content itself."""

    kind: str
    visibility: str  # public / internal — fixed by the source, never by config
    content: TextContent | SequenceContent


def clean_text(raw: str | None) -> str:
    """HTML → markdown + whitespace collapse: the canonical text that gets
    hashed and embedded."""
    text = html_to_markdown(raw)
    text = _SPACES_RE.sub(" ", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    return _BLANK_LINES_RE.sub("\n\n", text).strip()


def split_text(text: str, budget: int) -> list[str]:
    """Split text into pieces of at most `budget` chars: greedy packing of
    paragraphs, oversize paragraphs fall back to sentences, oversize
    sentences to a hard slice. Splits, never truncates."""
    text = text.strip()
    if not text:
        return []
    if len(text) <= budget:
        return [text]
    pieces: list[str] = []
    current = ""
    for atom in _atoms(text, budget):
        candidate = f"{current}\n\n{atom}" if current else atom
        if len(candidate) <= budget:
            current = candidate
        else:
            if current:
                pieces.append(current)
            current = atom
    if current:
        pieces.append(current)
    return pieces


def _atoms(text: str, budget: int) -> list[str]:
    """Paragraphs; oversize paragraphs become sentences; oversize sentences
    become hard budget-sized slices."""
    atoms: list[str] = []
    for para in text.split("\n\n"):
        para = para.strip()
        if len(para) <= budget:
            atoms.append(para)
            continue
        for sentence in _SENTENCE_RE.split(para):
            if len(sentence) <= budget:
                atoms.append(sentence)
            else:
                atoms.extend(sentence[i : i + budget] for i in range(0, len(sentence), budget))
    return [atom for atom in atoms if atom]


def chunk_object(
    fragments: Sequence[FragmentContent],
    *,
    max_chunk_tokens: int,
    items_per_window: int,
) -> list[Chunk]:
    """Chunk one object's already-resolved fragments.

    Empty content yields zero chunks of that fragment (e.g. the solution of an
    unresolved ticket) — an object with nothing to say produces nothing, it is
    not an error.
    """
    budget = max_chunk_tokens * CHARS_PER_TOKEN
    chunks: list[Chunk] = []
    for fragment in fragments:
        if isinstance(fragment.content, SequenceContent):
            chunks.extend(_window_chunks(fragment, budget=budget, per_window=items_per_window))
        else:
            chunks.extend(_text_chunks(fragment, fragment.content.text, budget=budget))
    return chunks


def _text_chunks(fragment: FragmentContent, text: str, *, budget: int) -> list[Chunk]:
    return [
        Chunk(kind=fragment.kind, n=n, text=piece, visibility=fragment.visibility, content_hash=_hash(piece))
        for n, piece in enumerate(split_text(text, budget))
    ]


def _window_chunks(fragment: FragmentContent, *, budget: int, per_window: int) -> list[Chunk]:
    """Windows of `per_window` items with boundaries fixed by item index.
    Windows are never re-split — instead each item is truncated to its share
    of the budget, so a window's boundaries depend on nothing but the index."""
    assert isinstance(fragment.content, SequenceContent)
    items = fragment.content.items
    item_budget = max(budget // per_window, 1)
    chunks = []
    for start in range(0, len(items), per_window):
        text = "\n".join(item[:item_budget] for item in items[start : start + per_window])
        chunks.append(
            Chunk(
                kind=fragment.kind,
                n=start // per_window,
                text=text,
                visibility=fragment.visibility,
                content_hash=_hash(text),
            )
        )
    return chunks


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
