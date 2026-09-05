"""The "relevant FAQ articles" scenario, as a value.

Mirrors `similar.py`'s "similar solved tickets" scenario — same reason to
exist apart from the tool that calls it: what intake considers a relevant FAQ
article is checkable without an LLM, without iTop and without a vector store.

No `exclude`: the ticket being processed is never itself an FAQ article. No
age window: stock iTop's `FAQ` carries no date at all
(`domain/faq_schema.py`), and an article going stale is not the same notion
as a solved ticket going stale — there is no analogue of
`similar_max_age_days` here.
"""

from itop_ai_assistant.vector import SearchQuery

from .config import IntakeConfig


def faq_query(cfg: IntakeConfig, *, text: str) -> SearchQuery:
    """The scenario for one ticket: FAQ articles that read like this one."""
    return SearchQuery(
        text=text,
        family=cfg.faq_family,
        # Omitted rather than `{"status": []}` when the deployment maps no
        # status of its own — an empty filter value is always a mistake
        # (`SearchQuery.__post_init__`), "unrestricted" is expressed by
        # leaving the key out.
        filters={"status": list(cfg.faq_statuses)} if cfg.faq_statuses else None,
        chunk_kinds=list(cfg.faq_chunk_kinds),
        # Explicit, not the query's default — the same safeguard `similar_query`
        # keeps against TASK-013 quoting internal correspondence.
        visibilities=["public"],
        min_score=cfg.faq_min_score,
        candidates=cfg.faq_candidates,
        top=cfg.faq_top,
    )
