"""Intake's own runtime-editable config section.

Resolved through `Settings.module_defaults` / `RedisConfigStore`, not a field
of `Settings` — see `settings/config_store.py`.
"""

from pydantic import BaseModel, Field, model_validator

_CLASSIFY_SERVICE_OQL = (
    "SELECT Service AS s"
    " JOIN lnkCustomerContractToService AS l1 ON l1.service_id=s.id"
    " JOIN CustomerContract AS cc ON l1.customercontract_id=cc.id"
    " WHERE cc.org_id = :this->org_id AND s.status != 'obsolete'"
)

_CLASSIFY_SUBCATEGORY_OQL = (
    "SELECT ServiceSubcategory"
    " WHERE service_id = :this->service_id"
    " AND (ISNULL(:this->request_type) OR request_type = :this->request_type)"
    " AND status != 'obsolete'"
)


class IntakeConfig(BaseModel):
    """The ticket-processing module: classify, ask, hand off.

    One tool-calling agent per ticket rather than a fixed sequence of steps —
    the model decides which tool to call next, the tools enforce the
    invariants.
    """

    # TODO: обернуть поля в Field с description, чтобы в UI не просто названия были, но и описание и может валидация.
    enabled: bool = True
    # The four actions the module may perform, switched independently. A
    # switched-off action is not asked to be skipped — its tool is never handed
    # to the model (ADR-012), so it cannot happen at all. Switching the module
    # off entirely is `enabled: false`, not clearing these.
    classify_enabled: bool = True
    clarify_enabled: bool = True
    handoff_note_enabled: bool = True
    similar_enabled: bool = True
    classes: list[str] = ["UserRequest", "Incident"]
    # The module acts on a ticket only while its status is in this list — an
    # intake concern (when this module may act), not the datamodel mapping's.
    active_statuses: list[str] = ["new"]
    max_rounds: int = 2
    max_classify_rounds: int = 2
    # Budget of model calls per run; without it a looping agent burns tokens
    # until the ticket is abandoned. Catalog + subcategories + classify +
    # similar tickets + question/handoff + slack.
    max_iterations: int = 9
    # One override for the whole module (the agent has a single loop); None
    # falls back to the global llm_model. It must be a reliable tool-caller —
    # a model that answers in prose instead of calling a tool burns the run.
    model: str | None = None
    classify_fallback_note: str = "Could not determine the request category. Manual classification required."
    handoff_fallback_note: str = "AI intake finished without a summary. Manual review required."
    # The vector family to search for similar solved tickets — intake's own
    # setting, not borrowed from `content_sources.tickets.FAMILY` (A8, rule
    # 8.1): the two happen to agree on "tickets" by convention, not by a
    # shared Python identifier, same relationship as `resolved_statuses` to
    # `VectorClassConfig.index_values` (ADR-017).
    similar_family: str = "tickets"
    # Similar solved tickets quoted in the handoff note (only when the vector
    # store and the embeddings endpoint are configured). The window is a range
    # over the modification date, never a substitute for the status filter —
    # a reopened ticket keeps its old resolution date (ADR-005, rule 2).
    similar_max_age_days: int = Field(default=365, gt=0)
    # Business parameter of the "similar solved" scenario — not tied to
    # `VectorClassConfig.index_values` (the matching default is a coincidence
    # for tickets, not a shared source of truth, see ADR-017).
    resolved_statuses: list[str] = ["resolved", "closed"]
    # Asked of the index; more than `similar_top` because candidates the
    # requester's iTop no longer returns are dropped afterwards (ADR-003)
    similar_candidates: int = Field(default=15, gt=0)
    similar_top: int = Field(default=5, gt=0)
    # Absolute floor on the Qdrant cosine score (range [-1, 1]) below which a
    # candidate is dropped regardless of rank — top-N alone does not
    # guarantee relevance, only relative rank among whatever `candidates`
    # happened to return (TASK-011). 0.6 is an engineering guess, not
    # calibrated against this deployment's embeddings model; tune it after a
    # live check against real similar/unrelated pairs.
    similar_min_score: float = Field(default=0.6, ge=-1.0, le=1.0)
    # Which chunk kinds the query text is matched against. The query is the new
    # ticket's title and description, so a match against `solution` means "the
    # solution reads like the problem" — usually noise, sometimes a genuine
    # restatement (TASK-012). Configurable because that call needs live tickets,
    # not a release. Non-empty: `search()` rejects an empty list loudly, and a
    # config value must not become a crash mid-run.
    similar_chunk_kinds: list[str] = Field(default=["profile", "body"], min_length=1)
    classify_service_oql: str = _CLASSIFY_SERVICE_OQL
    classify_subcategory_oql: str = _CLASSIFY_SUBCATEGORY_OQL

    @model_validator(mode="after")
    def _check_similar_budget(self) -> "IntakeConfig":
        """`similar_top` out of `similar_candidates`, never more.

        The same rule `SearchQuery` enforces, checked here as well because
        this is where an administrator can get it wrong: a bad pair is
        rejected at save time (422 from the admin API) instead of failing a
        run over a real ticket hours later.
        """
        if self.similar_top > self.similar_candidates:
            raise ValueError(
                f"similar_top ({self.similar_top}) exceeds similar_candidates ({self.similar_candidates}): "
                "candidates are only ever dropped when the requester's iTop no longer confirms them, "
                "so asking for more results than candidates cannot return them"
            )
        return self

    @model_validator(mode="after")
    def _check_action_toggles(self) -> "IntakeConfig":
        """Reject the two action combinations that cannot mean anything.

        Separate from `_check_similar_budget`: different rules about different
        fields, and one validator for both would answer two unrelated mistakes
        with one message.
        """
        if self.similar_enabled and not self.handoff_note_enabled:
            raise ValueError(
                "similar_enabled requires handoff_note_enabled: references to similar solved tickets "
                "exist only inside the handoff note, so searching for them without a note enriches nothing"
            )
        if not (self.classify_enabled or self.clarify_enabled or self.handoff_note_enabled):
            raise ValueError(
                "at least one of classify_enabled, clarify_enabled, handoff_note_enabled must stay on: "
                "switching the module off entirely is intake.enabled = false"
            )
        return self
