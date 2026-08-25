"""Intake's own runtime-editable config section.

Resolved through `Settings.module_defaults` / `RedisConfigStore`, not a field
of `Settings` — see `settings/config_store.py`.
"""

from pydantic import BaseModel, Field, field_validator, model_validator

from itop_ai_assistant.settings.ui_hints import ui

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

    Fields come in sections: the module as a whole first, then one section per
    action — its on/off switch followed by the settings that only matter while
    it is on. The field order is what the admin UI renders, so a section reads
    top to bottom as "turn this on, then tune it". A switched-off action is not
    asked to be skipped: its tool is never handed to the model (ADR-012), so it
    cannot happen at all. Switching the module off entirely is
    `enabled: false`, not clearing the four action switches.
    """

    # The module as a whole: whether it runs, on what, and its per-ticket
    # budgets.
    enabled: bool = Field(
        default=True,
        title="Enabled",
        description="Process newly created tickets. Read at startup: switching it needs a restart.",
    )
    classes: list[str] = Field(
        default=["UserRequest"],
        title="Ticket classes",
        description="iTop classes the module processes. Read at startup: a change needs a restart.",
    )
    # The module acts on a ticket only while its status is in this list — an
    # intake concern (when this module may act), not the datamodel mapping's.
    active_statuses: list[str] = Field(
        default=["new"],
        title="Act while the ticket is in status",
        description="Once an engineer moves the ticket out of these statuses, the module stops silently.",
    )
    # How many times the module may write to the requester about one ticket,
    # across both phases; `max_classify_questions` is the share of it that may
    # be spent while the ticket is still unclassified. What the completeness
    # phase is guaranteed is the difference between the two — a third "reserve"
    # field would state the same number twice and allow triples that contradict
    # each other.
    max_questions: int = Field(
        default=3,
        gt=0,
        title="Questions to the requester",
        description="How many times the module may write to the requester about one ticket, in total.",
    )
    # Budget of model calls per run; without it a looping agent burns tokens
    # until the ticket is abandoned. Catalog + subcategories + classify +
    # similar tickets + question/handoff + slack.
    max_iterations: int = Field(
        default=9,
        gt=0,
        title="Model calls per ticket",
        description="Budget for one run. When it runs out, the ticket gets the fallback note and is handed off.",
        json_schema_extra=ui(advanced=True),
    )
    # One override for the whole module (the agent has a single loop); None
    # falls back to the global llm_model. It must be a reliable tool-caller —
    # a model that answers in prose instead of calling a tool burns the run.
    model: str | None = Field(
        default=None,
        title="Model override",
        description="Leave empty to use the global model. Whatever is named here must call tools reliably.",
        json_schema_extra=ui(advanced=True),
    )

    # Classification: set the service and the subcategory of the ticket.
    classify_enabled: bool = Field(
        default=True,
        title="Enabled",
        description="Let the module set the service and the subcategory of the ticket.",
        json_schema_extra=ui(group="Classification", toggle=True),
    )
    max_classify_questions: int = Field(
        default=2,
        gt=0,
        title="Of them, before the ticket is classified",
        description="The share of the questions above that may be spent while the ticket is still unclassified.",
        json_schema_extra=ui(group="Classification"),
    )
    # Which catalog entries mean "nobody classified this" — an intake concern
    # (when this module has work to do), not the datamodel mapping's: the
    # mapping says which attribute holds the service, this says which values
    # inside it carry no classification.
    unclassified_service_ids: list[str] = Field(
        default=[],
        title='Services that mean "not classified"',
        description=(
            "Numeric IDs of the services that stand for a missing classification — the one a mail gateway "
            "fills in, for instance. A ticket carrying such a service is treated as unclassified together "
            "with its subcategory, and the service itself is never offered to the model. The ID is in the "
            "address bar when the service is open in iTop."
        ),
        json_schema_extra=ui(group="Classification"),
    )
    classify_service_oql: str = Field(
        default=_CLASSIFY_SERVICE_OQL,
        title="Service catalogue query",
        description="OQL for the services offered to the requester's organisation. `:this` is the ticket.",
        json_schema_extra=ui(group="Classification", widget="oql", advanced=True),
    )
    classify_subcategory_oql: str = Field(
        default=_CLASSIFY_SUBCATEGORY_OQL,
        title="Subcategory query",
        description="OQL for the subcategories of the service the module picked.",
        json_schema_extra=ui(group="Classification", widget="oql", advanced=True),
    )

    # Clarification: ask the requester in the public log, one question at a
    # time, within `max_questions`.
    clarify_enabled: bool = Field(
        default=True,
        title="Enabled",
        description="Let the module ask the requester in the public log, one question at a time.",
        json_schema_extra=ui(group="Clarifying questions", toggle=True),
    )

    # Handoff: the internal note that hands the ticket to an engineer.
    handoff_note_enabled: bool = Field(
        default=True,
        title="Enabled",
        description="Let the module write the internal note that hands the ticket to an engineer.",
        json_schema_extra=ui(group="Handoff note", toggle=True),
    )
    handoff_fallback_note: str = Field(
        default="AI intake finished without a summary. Manual review required.",
        title="Fallback note",
        description="Posted when the run ends without a summary of its own — out of budget, or nothing to add.",
        json_schema_extra=ui(group="Handoff note", widget="textarea"),
    )

    # Similar solved tickets, quoted inside the handoff note (only when the
    # vector store and the embeddings endpoint are configured).
    similar_enabled: bool = Field(
        default=True,
        title="Enabled",
        description=(
            "Let the module quote similar solved tickets in that note. Needs the handoff note, the vector "
            "index and an embeddings endpoint; without them the module simply does not search."
        ),
        json_schema_extra=ui(group="Similar solved tickets", toggle=True),
    )
    # The vector family to search — intake's own setting, not borrowed from
    # `content_sources.tickets.FAMILY` (A8, rule 8.1): the two happen to agree
    # on "tickets" by convention, not by a shared Python identifier, same
    # relationship as `resolved_statuses` to `VectorClassConfig.index_values`
    # (ADR-017).
    similar_family: str = Field(
        default="tickets",
        title="Index family",
        description="Which family of indexed content is searched.",
        json_schema_extra=ui(group="Similar solved tickets", advanced=True),
    )
    # Business parameter of the "similar solved" scenario — not tied to
    # `VectorClassConfig.index_values` (the matching default is a coincidence
    # for tickets, not a shared source of truth, see ADR-017).
    resolved_statuses: list[str] = Field(
        default=["resolved", "closed"],
        title="Statuses that count as solved",
        description="Only tickets in these statuses may be quoted.",
        json_schema_extra=ui(group="Similar solved tickets"),
    )
    # The window is a range over the modification date, never a substitute for
    # the status filter — a reopened ticket keeps its old resolution date
    # (ADR-005, rule 2).
    similar_max_age_days: int = Field(
        default=365,
        gt=0,
        title="Look back, days",
        description="How long ago a solved ticket may have been touched and still be quoted.",
        json_schema_extra=ui(group="Similar solved tickets"),
    )
    # Asked of the index; more than `similar_top` because candidates the
    # requester's iTop no longer returns are dropped afterwards (ADR-003)
    similar_candidates: int = Field(
        default=15,
        gt=0,
        title="Candidates read from the index",
        description=(
            "Larger than the number quoted on purpose: candidates the requester's iTop no longer shows "
            "are dropped afterwards."
        ),
        json_schema_extra=ui(group="Similar solved tickets", advanced=True),
    )
    similar_top: int = Field(
        default=5,
        gt=0,
        title="References in one note",
        description="At most this many similar tickets are quoted. Cannot exceed the number of candidates.",
        json_schema_extra=ui(group="Similar solved tickets"),
    )
    # Absolute floor on the Qdrant cosine score (range [-1, 1]) below which a
    # candidate is dropped regardless of rank — top-N alone does not
    # guarantee relevance, only relative rank among whatever `candidates`
    # happened to return (TASK-011). 0.6 is an engineering guess, not
    # calibrated against this deployment's embeddings model; tune it after a
    # live check against real similar/unrelated pairs.
    similar_min_score: float = Field(
        default=0.6,
        ge=-1.0,
        le=1.0,
        title="Minimum similarity",
        description=(
            "A candidate scoring below this is dropped whatever its rank. The default is a starting guess, "
            "not a value calibrated against this deployment's embeddings model."
        ),
        json_schema_extra=ui(group="Similar solved tickets", advanced=True),
    )
    # Which chunk kinds the query text is matched against. The query is the new
    # ticket's title and description, so a match against `solution` means "the
    # solution reads like the problem" — usually noise, sometimes a genuine
    # restatement (TASK-012). Configurable because that call needs live tickets,
    # not a release. Non-empty: `search()` rejects an empty list loudly, and a
    # config value must not become a crash mid-run.
    similar_chunk_kinds: list[str] = Field(
        default=["profile", "body"],
        min_length=1,
        title="Match against",
        description=(
            "Which parts of a solved ticket the new ticket's title and description are compared to. "
            "Adding `solution` matches problems against answers — usually noise."
        ),
        json_schema_extra=ui(group="Similar solved tickets", advanced=True),
    )

    @field_validator("unclassified_service_ids", mode="after")
    @classmethod
    def _check_service_ids(cls, value: list[str]) -> list[str]:
        """Numeric IDs only — a name here fails silently and forever.

        An administrator sees names in iTop, and "Mail request" typed into this
        field saves cleanly and never matches anything: the module would keep
        skipping exactly the tickets the field was filled in for. Saving is the
        one moment the mistake is still visible.
        """
        cleaned: list[str] = []
        for item in value:
            stripped = item.strip()
            # `isascii` as well: "²" and "١٢٣" are digits to Python and not IDs to iTop
            if not (stripped.isascii() and stripped.isdigit()):
                raise ValueError(
                    f"{item!r} is not a service ID: this field takes the numeric IDs of services, not their "
                    "names. Open the service in iTop and read `id=` from the address bar."
                )
            cleaned.append(stripped)
        return cleaned

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
    def _check_question_budget(self) -> "IntakeConfig":
        """The classification sub-limit lives inside the overall ceiling.

        Equality is allowed — "no reserve for the completeness phase" is a
        choice an administrator may make; a sub-limit above the ceiling is not,
        because the extra questions could never be asked.
        """
        if self.max_classify_questions > self.max_questions:
            raise ValueError(
                f"max_classify_questions ({self.max_classify_questions}) exceeds max_questions "
                f"({self.max_questions}): the requester is never asked more than max_questions times in total, "
                "so a larger classification sub-limit cannot be spent"
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
