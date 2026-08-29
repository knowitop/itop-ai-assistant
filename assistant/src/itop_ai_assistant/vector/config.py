"""Vector's own runtime-editable config section.

Resolved through `Settings.module_defaults` / `RedisConfigStore`, not a field
of `Settings` — see `settings/config_store.py`. `vector` is infrastructure,
not a business module (`.claude/rules/vector.md`), but resolves its section
through the same fallback a module's section does: `RedisConfigStore._defaults`
falls back to `module_defaults` for any section `Settings` has no attribute
for, not only for a registered module. `vector/__init__.py` re-exports these
four names — a business module importing them reaches through the facade,
never this module directly.
"""

from pydantic import BaseModel, Field


class ChunkFragmentConfig(BaseModel):
    """What an administrator may say about one chunk fragment.

    Which of the two keys is read depends on how the source declared the
    fragment (`vector/ports/source.py::FragmentSpec`, ADR-018): a required fragment
    reads `fields`, an opt-in one reads `enabled` and has no fields of its own
    — its content is the source's business. The fragment's kind and its
    visibility are not here at all and cannot be: they belong to the source.
    """

    # Semantic field names, from the source's `fields` vocabulary. Empty means
    # the fragment produces nothing — valid, but almost always a mistake.
    fields: list[str] = []
    # Only meaningful for opt-in fragments; a fragment missing from the
    # `chunks` map entirely is off.
    enabled: bool = True


class VectorClassConfig(BaseModel):
    """Per-class vector index settings (one entry per indexed object class,
    nested under the family — `vector.families[<family>].classes[<class>]` —
    that owns it, ADR-015/TASK-021: the family, not the class, is the real
    unit of grouping everywhere else in the architecture).

    Every indexed class must expose a last-modification datetime and a
    "relevance" attribute — the VectorSource contract (`vector/ports/source.py`).
    Which attributes those are is the source's concern (tickets map them via
    `ticket_mapping`); this config holds only the relevance *values*.
    """

    # Values of the class's relevance attribute that keep an object in the
    # index (similar-tickets searches want resolved knowledge, not open
    # noise); [] = index every object of the class
    index_values: list[str] = []
    # Chunking settings keyed by fragment kind. The keys are not free-form:
    # they must be fragments the class's source declares, and a key it does
    # not know is ignored with a warning at sweep time.
    chunks: dict[str, ChunkFragmentConfig] = {}


_TICKET_CHUNKS = {
    "profile": ChunkFragmentConfig(fields=["title", "service", "subcategory"]),
    "body": ChunkFragmentConfig(fields=["description"]),
    "solution": ChunkFragmentConfig(fields=["solution"]),
    # The two log fragments are opt-in and deliberately absent: indexing
    # internal notes is a privacy decision, not a default (TASK-013).
}

_FAQ_CHUNKS = {
    "profile": ChunkFragmentConfig(fields=["title", "summary", "category_name", "error_code", "key_words"]),
    "body": ChunkFragmentConfig(fields=["description"]),
}


class FamilyConfig(BaseModel):
    """Per-family vector index settings — one entry per `VectorSource.name`
    (ADR-015: one collection per family, `dev-docs/tasks/TASK-021-*`).

    The family, not the class, is the unit `sweep_interval_seconds` and
    `log_entries_per_chunk` actually belong to: both are about how one
    collection's sweep behaves (incremental cursor overlap, log-window
    chunking), and a source without a cheap incremental scan (FAQ has no
    `last_update`) may want a slower cadence than the rest of the deployment
    without slowing everything else down.
    """

    # Off = the sweep skips this family whole (no prepare, no ensure_version,
    # no cursor touched), reconciliation leaves it alone, and a search over it
    # is refused instead of answering from a collection nothing refreshes.
    # The collection itself stays: switching back on resumes the increment
    # where it stopped, dropping it is a separate manual operation.
    enabled: bool = True
    # Classes this family indexes, each with its own relevance values and
    # chunk fragment settings. The family key must match a registered
    # `VectorSource.name` (`content_sources/registry.py`) to do anything —
    # same tolerance as an unknown class today: a key that matches nothing
    # is logged and skipped, not rejected.
    classes: dict[str, VectorClassConfig] = {}
    # None = use VectorConfig's system-wide value.
    sweep_interval_seconds: int | None = Field(default=None, gt=0)
    log_entries_per_chunk: int | None = Field(default=None, gt=0)


class VectorConfig(BaseModel):
    """Vector index settings — infrastructure section "vector" (setup API).

    Off by default: the base deployment stays Redis-only. The chunking
    profiles and sweep settings are consumed by the indexer (Stage 2);
    they live here from the start so the section schema is stable.
    """

    enabled: bool = False
    # Indexed families (one Qdrant collection each, ADR-015) with their
    # per-family settings.
    families: dict[str, FamilyConfig] = {
        "tickets": FamilyConfig(
            classes={
                "UserRequest": VectorClassConfig(index_values=["resolved", "closed"], chunks=_TICKET_CHUNKS),
            }
        ),
        "faq": FamilyConfig(
            classes={
                # No status attribute for FAQ in stock iTop — [] indexes every
                # article (ADR-005: "no attribute to filter by" degrades to
                # "index everything", not an error). Set explicit values if a
                # deployment adds a status.
                "FAQ": VectorClassConfig(index_values=[], chunks=_FAQ_CHUNKS),
            }
        ),
    }
    sweep_interval_seconds: int = Field(default=300, gt=0)
    sweep_page_size: int = Field(default=100, gt=0)
    # Pause between iTop pages so a backfill doesn't hammer the REST API
    sweep_throttle_seconds: float = Field(default=0.5, ge=0)
    reconcile_interval_days: int = Field(default=7, gt=0)
    max_chunk_tokens: int = Field(default=480, gt=0)
    # An object chunking past this is skipped whole, before anything is
    # embedded — the embeddings endpoint is billed per call, and an object
    # this size is junk (a base64 attachment, a minified asset in the body),
    # not a long article. The default is derived from `max_chunk_tokens`: at
    # 480 tokens a chunk holds ~1440 characters (`chunker.CHARS_PER_TOKEN`),
    # so 32 chunks is a ~46 000-character document — an order of magnitude
    # past any real FAQ article, and two orders below an inlined screenshot.
    max_chunks_per_object: int = Field(default=32, gt=0)
    log_entries_per_chunk: int = Field(default=5, gt=0)
