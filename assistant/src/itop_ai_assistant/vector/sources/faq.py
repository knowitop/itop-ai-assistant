"""FAQ vector source: iTop FAQ articles as vectorizable objects.

Wraps `FaqRepository` behind the generic `VectorSource` protocol
(`vector/ports/source.py`) — see `vector/sources/tickets.py` for the same
pattern applied to tickets. Simpler than tickets: one class, no catalog
lookups, no log fragments (an FAQ article has no conversation to index).
"""

import logging
from collections.abc import Sequence
from datetime import datetime

from itop_ai_assistant.config import ChunkFragmentConfig, VectorClassConfig
from itop_ai_assistant.domain.faq import FaqArticle
from itop_ai_assistant.repositories.faq import FaqRepository, FaqRepositoryProvider
from itop_ai_assistant.vector.chunker import Chunk, FragmentContent, TextContent, chunk_object, clean_text
from itop_ai_assistant.vector.ports.source import FragmentSpec, VectorRecord, VectorSource

logger = logging.getLogger(__name__)

# The collection family this source writes to (`FaqVectorSource.name`).
FAMILY = "faq"

# The semantic fields an administrator composes the required fragments from
# (ADR-018). Not iTop attribute names: the mapping to those is
# `FaqMappingConfig`'s job.
FIELDS = ("title", "summary", "category_name", "error_code", "key_words", "description")

FRAGMENTS = (
    FragmentSpec(kind="profile", visibility="public"),
    FragmentSpec(kind="body", visibility="public"),
)


class FaqVectorSource(VectorSource[FaqArticle]):
    """VectorSource implementation for iTop FAQ articles.

    Source contract (`vector/ports/source.py`): the relevance attribute is the
    semantic `status`, the modification date is `last_update` — both mapped
    to actual iTop attributes by `FaqMappingConfig`, both unmapped by default.
    Stock iTop's `FAQ` class carries neither a lifecycle status nor any date
    attribute at all: `vector.families.faq.classes.FAQ.index_values` is `[]`
    (every article stays in the index) and `FaqRepository.find_modified_since` always does a
    full scan (see its docstring) — a deployment whose `FAQ` does carry either
    can map it and set `index_values` explicitly.

    `org_id` is unmapped by default too — stock `FAQ` has no org-scoped ACL —
    so the R4 pre-filter by organization (ADR-003) is simply inactive for
    every article until a deployment maps a real attribute in `faq_mapping`.
    `indexed_filter_keys` still declares it: an unused payload index on an
    always-empty key costs a little Qdrant bookkeeping, not an error, and a
    deployment that does map `org_id` needs no further code change to make
    the pre-filter work.
    """

    name = FAMILY
    indexed_filter_keys = ("org_id",)
    fields = FIELDS
    fragments = FRAGMENTS

    def __init__(self, get_faq_repo: FaqRepositoryProvider, *, classes: list[str]) -> None:
        self._get_faq_repo = get_faq_repo
        self.classes: Sequence[str] = classes
        self._faq_repo: FaqRepository | None = None

    async def prepare(self) -> None:
        # The plain connection, not a principal's view of it — see
        # `TicketVectorSource.prepare` for the same reasoning: the sweep is
        # not a run, and the index it builds is global by design.
        self._faq_repo = await self._get_faq_repo()

    async def find_modified_since(
        self, obj_class: str, since: datetime | None, *, page: int, page_size: int
    ) -> list[VectorRecord[FaqArticle]]:
        assert self._faq_repo is not None, "prepare() must run before find_modified_since()"
        articles = await self._faq_repo.find_modified_since(since, page=page, page_size=page_size)
        return [
            VectorRecord(
                obj_id=int(article.id),
                index_value=article.status,
                updated_at=article.last_update,
                created_at=article.start_date,
                org_id=article.org_id,
                payload=article,
            )
            for article in articles
        ]

    async def find_existing_ids(self, obj_class: str, ids: list[int]) -> set[int]:
        assert self._faq_repo is not None, "prepare() must run before find_existing_ids()"
        return await self._faq_repo.find_existing_ids(ids)

    async def chunk(
        self,
        obj_class: str,
        record: VectorRecord[FaqArticle],
        cfg: VectorClassConfig,
        *,
        max_chunk_tokens: int,
        log_entries_per_chunk: int,
    ) -> list[Chunk]:
        article = record.payload
        fields = self._semantic_fields(article)
        fragments = [
            content
            for spec in FRAGMENTS
            if (content := self._resolve(spec, cfg.chunks.get(spec.kind), fields)) is not None
        ]
        return chunk_object(fragments, max_chunk_tokens=max_chunk_tokens, items_per_window=log_entries_per_chunk)

    def _semantic_fields(self, article: FaqArticle) -> dict[str, str]:
        """`FIELDS` bound to this article's content, canonicalized."""
        return {
            "title": clean_text(article.title),
            "summary": clean_text(article.summary),
            "category_name": clean_text(article.category_name),
            "error_code": clean_text(article.error_code),
            "key_words": clean_text(article.key_words),
            "description": clean_text(article.description),
        }

    def _resolve(
        self, spec: FragmentSpec, cfg: ChunkFragmentConfig | None, fields: dict[str, str]
    ) -> FragmentContent | None:
        """One fragment's configured content, or None if it produces nothing."""
        if cfg is None or not cfg.fields:
            return None
        parts = []
        for name in cfg.fields:
            if name not in fields:
                logger.warning(f"faq source: fragment {spec.kind!r} references unknown field {name!r} — ignored")
                continue
            if fields[name]:
                parts.append(fields[name])
        return FragmentContent(spec.kind, spec.visibility, TextContent("\n\n".join(parts)))
