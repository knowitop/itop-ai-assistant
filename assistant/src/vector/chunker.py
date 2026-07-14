"""Object → chunks: pure, deterministic functions (no I/O), source-agnostic.

Determinism matters: the sweep's hash-guard compares sha256 of the *final*
chunk text against what is stored, so the same input must always produce
byte-identical chunks — cosmetic HTML churn is neutralized by `clean_text`,
and the split algorithm has no randomness.

Chunk kinds are the keys of the per-class `vector.profiles` config
(`profile` / `body` / `solution`, log kinds `log:public` / `log:private`) —
the config is the source of truth, there is no mapping layer. This module has
no domain imports: sources (e.g. `vector_sources/tickets.py`) translate their
own field/log shapes into the `fields`/`logs` dicts consumed here.
"""

import hashlib
import logging
import re
from dataclasses import dataclass

from text_utils import html_to_markdown

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
class ConversationEntry:
    """One turn of a log/conversation-shaped source, already resolved to a
    generic role by the caller (e.g. ticket "caller"/"agent" — this module
    doesn't know what a caller is)."""

    speaker: str
    message: str


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
    fields: dict[str, str],
    profile: dict[str, list[str]],
    *,
    max_chunk_tokens: int,
    log_entries_per_chunk: int,
    logs: dict[str, list[ConversationEntry]] | None = None,
) -> list[Chunk]:
    """Chunk one object according to its class profile.

    `fields` holds cleaned-or-raw semantic field texts (values are passed
    through `clean_text`); `logs` maps log kinds ("log:public"/"log:private")
    to their entries, already role-labeled by the caller. An empty source
    (e.g. solution of an unresolved ticket) yields zero chunks of that kind.
    """
    budget = max_chunk_tokens * CHARS_PER_TOKEN
    chunks: list[Chunk] = []
    for kind, sources in profile.items():
        if kind.startswith("log:"):
            entries = (logs or {}).get(kind, [])
            chunks.extend(_log_chunks(kind, entries, budget=budget, per_chunk=log_entries_per_chunk))
            continue
        parts = []
        for name in sources:
            if name not in fields:
                logger.warning(f"chunker: profile kind {kind!r} references unknown field {name!r} — treated as empty")
                continue
            cleaned = clean_text(fields[name])
            if cleaned:
                parts.append(cleaned)
        for n, piece in enumerate(split_text("\n\n".join(parts), budget)):
            chunks.append(Chunk(kind=kind, n=n, text=piece, visibility="public", content_hash=_hash(piece)))
    return chunks


def _log_chunks(kind: str, entries: list[ConversationEntry], *, budget: int, per_chunk: int) -> list[Chunk]:
    """Windows of `per_chunk` entries with boundaries fixed by entry index:
    appending entries only changes the last window, so earlier chunks' hashes
    never move (no re-embedding of old history). Windows are never re-split —
    instead each entry is truncated to its share of the budget."""
    visibility = "internal" if kind == "log:private" else "public"
    entry_budget = max(budget // per_chunk, 1)
    chunks = []
    for start in range(0, len(entries), per_chunk):
        lines = []
        for entry in entries[start : start + per_chunk]:
            lines.append(f"{entry.speaker}: {clean_text(entry.message)[:entry_budget]}")
        text = "\n".join(lines)
        chunks.append(
            Chunk(kind=kind, n=start // per_chunk, text=text, visibility=visibility, content_hash=_hash(text))
        )
    return chunks


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
