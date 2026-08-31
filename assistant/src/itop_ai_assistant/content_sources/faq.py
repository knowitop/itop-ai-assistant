"""FAQ vector source: iTop FAQ articles as vectorizable objects.

Wraps `FaqRepository` behind the generic `VectorSource` protocol
(`vector/ports/source.py`) — see `content_sources/tickets.py` for the same
pattern applied to tickets. Simpler than tickets: one class, no catalog
lookups, no log fragments (an FAQ article has no conversation to index).
"""

import logging
from collections.abc import Sequence
from datetime import datetime

from itop_ai_assistant.content_sources.acl import org_ids
from itop_ai_assistant.core.principal import Principal
from itop_ai_assistant.domain.faq import FaqArticle
from itop_ai_assistant.repositories.faq import FaqRepository, FaqRepositoryForPrincipal, FaqRepositoryProvider
from itop_ai_assistant.vector import (
    Chunk,
    ChunkPlan,
    FamilyConfig,
    FragmentContent,
    FragmentSpec,
    TextContent,
    VectorRecord,
    VectorSource,
    chunk_object,
    clean_text,
)

logger = logging.getLogger(__name__)

# The collection family this source writes to (`FaqVectorSource.name`).
FAMILY = "faq"

# The semantic fields an administrator composes the required fragments from
# (ADR-018). Not iTop attribute names: the mapping to those is
# `FaqMappingConfig`'s job.
FIELDS = ("title", "summary", "category_name", "error_code", "key_words", "description")

# The semantic fields of an article that can name an organization giving
# access to it (`VectorClassConfig.acl_org_fields` picks from these). Both are
# unmapped in stock iTop: `org_id` is the article's own organization,
# `customer_org_ids` the list of customer organizations a build publishes it
# to (mapped as `customers_list:customer_id`, see `repositories/valuemap.py`).
ORG_FIELDS = ("org_id", "customer_org_ids")

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

    Neither of `ORG_FIELDS` is mapped by default — stock `FAQ` has no
    org-scoped ACL — so `vector.families.faq.classes.FAQ.acl_org_fields` is
    empty and the R4 pre-filter (ADR-003) lets every article through to
    `confirm_visible`. A build that does publish articles to a list of
    customer organizations maps that link set onto `customer_org_ids` in
    `faq_mapping` and names the field here; no code change is needed for
    either.
    """

    name = FAMILY
    fields = FIELDS
    org_fields = ORG_FIELDS
    fragments = FRAGMENTS

    def __init__(
        self,
        get_faq_repo: FaqRepositoryProvider,
        get_faq_repo_as: FaqRepositoryForPrincipal,
        *,
        family_cfg: FamilyConfig,
    ) -> None:
        self._get_faq_repo = get_faq_repo
        self._get_faq_repo_as = get_faq_repo_as
        self.classes: Sequence[str] = list(family_cfg.classes)
        # See `TicketVectorSource.__init__` — per class, which of `ORG_FIELDS`
        # this deployment says grant access.
        self._acl_org_fields = {name: cfg.acl_org_fields for name, cfg in family_cfg.classes.items()}
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
                acl_org_ids=org_ids(article, self._acl_org_fields.get(obj_class, ()), source=FAMILY),
                payload=article,
            )
            for article in articles
        ]

    async def find_existing_ids(self, obj_class: str, ids: list[int]) -> set[int]:
        assert self._faq_repo is not None, "prepare() must run before find_existing_ids()"
        return await self._faq_repo.find_existing_ids(ids)

    async def confirm_visible(self, principal: Principal, obj_class: str, ids: list[int]) -> set[int]:
        # `obj_class` is dropped, same as above: this source owns one class,
        # and `FaqRepository` queries `FAQ` by name. See
        # `TicketVectorSource.confirm_visible` for why the repository is
        # fetched per call rather than cached.
        repo = await self._get_faq_repo_as(principal)
        return await repo.find_existing_ids(ids)

    async def chunk(
        self,
        obj_class: str,
        record: VectorRecord[FaqArticle],
        plan: ChunkPlan,
        *,
        max_chunk_tokens: int,
        log_entries_per_chunk: int,
    ) -> list[Chunk]:
        article = record.payload
        fields = self._semantic_fields(article)
        fragments = [content for spec in FRAGMENTS if (content := self._resolve(spec, plan, fields)) is not None]
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

    def _resolve(self, spec: FragmentSpec, plan: ChunkPlan, fields: dict[str, str]) -> FragmentContent | None:
        """One fragment's configured content, or None if it produces nothing."""
        field_names = plan.fields.get(spec.kind)
        if not field_names:
            return None
        parts = []
        for name in field_names:
            if name not in fields:
                logger.warning(f"faq source: fragment {spec.kind!r} references unknown field {name!r} — ignored")
                continue
            if fields[name]:
                parts.append(fields[name])
        return FragmentContent(spec.kind, spec.visibility, TextContent("\n\n".join(parts)))
