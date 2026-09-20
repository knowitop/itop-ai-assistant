"""The "relevant FAQ articles" scenario, as a value.

Mirrors `similar.py`'s "similar solved tickets" scenario — same reason to
exist apart from the tool that calls it: what intake considers a relevant FAQ
article is checkable without an LLM, without iTop and without a vector store.

No `exclude`: the ticket being processed is never itself an FAQ article. No
age window: stock iTop's `FAQ` carries no date at all
(`domain/faq_schema.py`), and an article going stale is not the same notion
as a solved ticket going stale — there is no analogue of
`similar_max_age_days` here.

`org_ids` is the one thing `similar_query` has no counterpart for: an article
may be published to a list of customer organizations, and one published to
somebody else's is not an answer to this ticket, however well it reads
(ADR-033, ADR-034).
"""

from collections.abc import Sequence

from itop_ai_assistant.vector import SearchQuery

from .config import IntakeConfig


def faq_query(cfg: IntakeConfig, *, text: str, org_ids: Sequence[str] | None) -> SearchQuery:
    """The scenario for one ticket: FAQ articles that read like this one.

    `org_ids` are the organizations the ticket belongs to, and articles are
    kept for them the way ADR-033 defines the pre-filter: an article whose
    `acl_org_ids` is empty is published to everybody and passes, one that
    names organizations passes only if it names one of these. `None` — the
    ticket names no organization — means no pre-filter at all: "only the
    unrestricted articles" is not expressible here, and a ticket without an
    organization is degenerate data in iTop rather than a scenario to design
    for.

    A parameter rather than a setting, and required rather than defaulted:
    which corpus is the ticket's own is a fact about correctness, not
    something a deployment may switch off, and a caller that forgot it should
    not silently get everybody's articles.
    """
    return SearchQuery(
        text=text,
        family=cfg.faq_family,
        org_ids=list(org_ids) if org_ids else None,
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
