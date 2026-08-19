"""The "similar solved tickets" scenario, as a value.

Intake's search is configuration, not code: every parameter below already
lives in `IntakeConfig`, and this module is the one place that turns that
configuration into a `SearchQuery` the vector subsystem accepts whole
(TASK-031, rule 6.6). The tool that calls the search reads no config of its
own — it asks for the scenario and passes it on.

Being a pure function is the point: what intake considers a similar solved
ticket is checkable without an LLM, without iTop and without a vector store.
"""

from datetime import datetime, timedelta

from itop_ai_assistant.vector import DateRange, SearchQuery

from .config import IntakeConfig


def similar_query(cfg: IntakeConfig, *, text: str, exclude: tuple[str, int], now: datetime) -> SearchQuery:
    """The scenario for one ticket: solved tickets that read like this one.

    `now` is a parameter rather than read from the clock here so the age
    window is testable without patching time.
    """
    return SearchQuery(
        text=text,
        family=cfg.similar_family,
        filters={"status": list(cfg.resolved_statuses)},
        chunk_kinds=list(cfg.similar_chunk_kinds),
        # Explicit, not the query's default: the index gets private log chunks
        # in TASK-013, and intake must not start quoting internal
        # correspondence without a change here.
        visibilities=["public"],
        exclude=exclude,
        # Lower bound only — "solved recently"; the window has an upper bound
        # available (TASK-018) and intake has no use for one.
        updated=DateRange(after=now - timedelta(days=cfg.similar_max_age_days)),
        min_score=cfg.similar_min_score,
        candidates=cfg.similar_candidates,
        top=cfg.similar_top,
    )
