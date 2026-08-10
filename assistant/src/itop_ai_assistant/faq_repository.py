from collections.abc import Awaitable, Callable
from datetime import datetime

from itop_ai_assistant.config import FaqMappingConfig
from itop_ai_assistant.domain.faq import FaqArticle
from itop_ai_assistant.itop_client import Itop
from itop_ai_assistant.text_utils import ITOP_DATETIME_FORMAT, parse_itop_dt


class FaqRepository:
    """Translates between the semantic FaqArticle model and raw iTop attributes.

    Read-only: the vector sweep only ever reads FAQ content, it never writes
    to it — unlike `TicketRepository`, there is no `set_fields`/`append_log`
    here.
    """

    def __init__(self, itop: Itop, mapping: FaqMappingConfig):
        self._itop = itop
        self.mapping = mapping

    def to_article(self, raw: dict) -> FaqArticle:
        fields = self.mapping.fields.model_dump()

        def attr(semantic: str):
            attr_code = fields.get(semantic)
            return raw.get(attr_code) if attr_code else None

        return FaqArticle(
            id=str(raw["id"]),
            title=attr("title") or "",
            summary=attr("summary") or "",
            category_name=attr("category_name") or "",
            error_code=attr("error_code") or "",
            key_words=attr("key_words") or "",
            description=attr("description") or "",
            status=attr("status") or "",
            org_id=attr("org_id"),
            last_update=parse_itop_dt(attr("last_update")),
            start_date=parse_itop_dt(attr("start_date")),
        )

    async def find_modified_since(self, since: datetime | None, *, page: int, page_size: int) -> list[FaqArticle]:
        """One page of FAQ articles modified at/after `since` (None = full scan).

        Deliberately no status predicate: an article that left the indexable
        scope must still be seen so its chunks can be deleted (same reasoning
        as `TicketRepository.find_modified_since`).

        Unlike tickets, an unmapped `last_update` is not a config mistake —
        stock iTop's `FAQ` class carries no date attribute at all. When that
        is the case this always does a full scan (`since` is ignored): there
        is nothing to filter by, so every sweep pass re-reads every article.
        The hash-guard (`vector/indexer.py`) keeps that cheap — unchanged
        chunks are neither re-embedded nor rewritten, only re-read.
        """
        fields = self.mapping.fields.model_dump()
        last_update_attr = fields.get("last_update")
        query = (
            {}
            if last_update_attr is None or since is None
            else {last_update_attr: (">=", since.strftime(ITOP_DATETIME_FORMAT))}
        )
        attrs = [attr for attr in fields.values() if attr]
        rows = await self._itop.schema("FAQ").find(
            query, projection=["id", *attrs], limit=str(page_size), page=str(page)
        )
        return [self.to_article(row) for row in rows]

    async def find_existing_ids(self, ids: list[int]) -> set[int]:
        """Which of the given ids still exist in iTop (reconciliation probe)."""
        if not ids:
            return set()
        id_list = ",".join(str(int(i)) for i in ids)
        rows = await self._itop.schema("FAQ").find(f"SELECT FAQ WHERE id IN ({id_list})", projection=["id"])
        return {int(row["id"]) for row in rows}


# Same reasoning as `TicketRepoFactory` in `ticket_repository.py`: the shape
# of "a way to fetch a fresh `FaqRepository`", declared once instead of
# redeclared by each caller.
type FaqRepoFactory = Callable[[], Awaitable[FaqRepository]]
