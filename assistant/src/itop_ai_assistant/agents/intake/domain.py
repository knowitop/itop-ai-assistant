"""Intake's own business rules (rule 2.1), pure and framework-free.

`tools.py` and `run.py` call these and translate the outcome into I/O, a
`ToolRejection` or a skip — they do not compare thresholds, parse `.strip()`
or inspect the ticket/log themselves.
"""

from dataclasses import dataclass
from enum import Enum, auto

from itop_ai_assistant.domain.ticket import Ticket

from .state import TicketState


@dataclass(frozen=True)
class IntakeScope:
    """Which of intake's four actions this run may perform.

    An administrator's answer, read once per run: the tool set, the prompt and
    the journal all take it from here instead of each reading `IntakeConfig`
    on its own and drifting apart. Built in `compose.py` — this module knows
    nothing about configuration.

    `similar` already folds in whether the deployment can search at all, the
    other three are the plain switches.
    """

    classify: bool
    clarify: bool
    handoff_note: bool
    similar: bool


@dataclass(frozen=True)
class Classification:
    """Which catalog services count as a classification at all.

    A deployment's answer, read once per run alongside `IntakeScope`. Mandatory
    fields make every ticket look classified: a mail gateway has to put
    *something* in the service, and the ticket arrives carrying a service that
    means "nobody looked at this yet". Those services are declared here, and
    everything that asks about the ticket's classification asks through this
    object rather than reading `Ticket.service_id` raw.

    Not a property of `Ticket`: the model knows nothing about configuration,
    and "this value means nothing" is intake's reading of a ticket, not a fact
    about it — pattern analysis will want those very tickets as a group.
    """

    unclassified_services: frozenset[str] = frozenset()

    def service_of(self, ticket: Ticket) -> str | None:
        """The ticket's real service, or None where there is none."""
        if ticket.service_id is None or ticket.service_id in self.unclassified_services:
            return None
        return ticket.service_id

    def subcategory_of(self, ticket: Ticket) -> str | None:
        """The ticket's real subcategory, or None where there is none.

        A subcategory belongs to exactly one service (mandatory external key in
        iTop, and a ticket cannot hold a subcategory of another service), so a
        declared service takes its subcategories down with it and none of them
        has to be declared separately. If the two ever disagree — a subcategory
        left over from an earlier service — the answer is the same one: this
        ticket needs classifying again.
        """
        if self.service_of(ticket) is None:
            return None
        return ticket.subcategory_id


def needs_classification(scope: IntakeScope, ticket: Ticket, classification: Classification) -> bool:
    """Is classifying this ticket both allowed and still to be done?

    The only place where "the administrator switched it on" meets "the ticket
    is not classified yet". Both the tool set and the prompt ask this — and so
    does `post_public_question`, once, when it records which phase the question
    was asked in.
    """
    return scope.classify and not (classification.service_of(ticket) and classification.subcategory_of(ticket))


def finish_tool(scope: IntakeScope) -> str:
    """The name of the one tool that ends a session under this scope.

    Said out loud by everything that has to point a run at its way out — the
    line closing a classification, a refusal — so the name is decided here
    rather than re-derived from `handoff_note` at each site.
    """
    return "finish_handoff" if scope.handoff_note else "finish_processing"


def closing_tools(scope: IntakeScope) -> list[str]:
    """Names of the tools that can end a session under this scope.

    One of them always exists: `finish_processing` stands in wherever the
    handoff note is switched off, so a run is never left without a way out.
    Tool names rather than actions because everything that says this out loud
    — the prompt, a refusal, the line closing a classification — says it to
    the model, which knows tools.
    """
    names = ["post_public_question"] if scope.clarify else []
    names.append(finish_tool(scope))
    return names


def format_scope(scope: IntakeScope) -> str:
    """`classify=on clarify=off …` — the journal's record of what a run could do.

    Without it "the agent did nothing" is indistinguishable from a breakage.
    """
    flags = {
        "classify": scope.classify,
        "clarify": scope.clarify,
        "handoff_note": scope.handoff_note,
        "similar": scope.similar,
    }
    return " ".join(f"{name}={'on' if enabled else 'off'}" for name, enabled in flags.items())


def format_incoming_classification(ticket: Ticket, classification: Classification) -> str:
    """`service=unclassified(7) subcategory=42` — what the ticket arrived with.

    Written whether or not this run classifies: whoever measures how many
    tickets reach the module unclassified needs the denominator, and after
    `set_classification` the ticket no longer remembers what it looked like.

    Only the service can carry the marker — it is the one that decides, and
    the subcategory is printed raw so the line still says what stands in iTop.
    """
    service_id = ticket.service_id
    if service_id is None:
        service = "none"
    elif classification.service_of(ticket) is None:
        service = f"unclassified({service_id})"
    else:
        service = service_id
    return f"service={service} subcategory={ticket.subcategory_id or 'none'}"


class QuestionBudget(Enum):
    """What `post_public_question` may do, given the questions already asked."""

    OK = auto()  # under budget — post the question
    CLASSIFY_EXHAUSTED = auto()  # the classification sub-limit is spent, the category is still unknown
    EXHAUSTED = auto()  # no questions left for this ticket at all


def check_question_budget(
    state: TicketState, *, classifying: bool, max_questions: int, max_classify_questions: int
) -> QuestionBudget:
    """One ceiling on questions to the requester, with a sub-limit inside it.

    The order matters: a ticket that hits the overall ceiling while still
    unclassified is told the category could not be determined, not that it has
    enough detail. Both outcomes end the questioning, they just say different
    things to the model.
    """
    if classifying and state.classify_questions_asked >= max_classify_questions:
        return QuestionBudget.CLASSIFY_EXHAUSTED
    if state.questions_asked >= max_questions:
        return QuestionBudget.EXHAUSTED
    return QuestionBudget.OK


@dataclass(frozen=True)
class NonBlankText:
    """A question or a note is not an answer if there is nothing in it."""

    value: str

    def __post_init__(self) -> None:
        if not self.value.strip():
            raise ValueError("text is blank")


def stop_reason(ticket: Ticket, state: TicketState, *, active_statuses: list[str], ai_name: str) -> str | None:
    """Why this ticket must not be processed, or None to proceed."""
    if state.ai_done:
        return "already processed (ai_done)"
    if ticket.status not in active_statuses:
        return f"status={ticket.status} not in {active_statuses}"
    # Loop protection, second line of defense after iTop trigger contexts:
    # if our own question is the last public entry, wait for the user instead
    # of reacting to our own comment or a duplicate webhook.
    if ticket.public_log and ticket.public_log[-1].user_login == ai_name:
        return "last public entry is ours, waiting for the requester"
    return None
