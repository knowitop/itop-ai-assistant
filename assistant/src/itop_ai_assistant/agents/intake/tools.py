"""The intake agent's tools — one per invariant it must not be able to break.

Docstrings are code, not `prompts/`: they must stay in sync with the
signatures and with the rules enforced inside. Each one says *when* to call
the tool and what to do afterwards.

Every refusal is a `ToolRejection` carrying an explicit "do this instead" —
without it a model repeats the same call until the budget runs out. Real
failures (iTop down, Redis down) are not caught here: they propagate and fail
the run.
"""

import logging
from datetime import UTC, datetime, timedelta
from typing import TypeAlias

from langchain.tools import ToolRuntime
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import BaseTool, tool

from itop_ai_assistant.domain.ticket import Ticket
from itop_ai_assistant.text_utils import bind_oql, html_to_markdown

from .context import IntakeContext
from .prompt import format_options

logger = logging.getLogger(__name__)

# `TypeAlias` is not decoration: the pre-commit mypy runs in its own venv
# without langchain, so `ToolRuntime` resolves to Any and a plain assignment
# would read as a variable ("not valid as a type") in every signature below.
IntakeToolRuntime: TypeAlias = ToolRuntime[IntakeContext]


class ToolRejection(Exception):
    """A tool refused the call. Becomes an error ToolMessage for the model."""


# Closing line of a successful `set_classification`. Deliberately a statement
# about the process, not an instruction to the reader: the earlier wording
# ("Now check the ticket… ask the requester… or write the note") read as a
# conversational turn, and in three of the four observed prose-instead-of-a-
# tool-call slips the model answered it in prose. Keep it impersonal — no
# second person, no imperative verbs, no question.
_REMAINS = "Remaining for this session: exactly one call, post_public_question or finish_handoff."


def _reject_if_repeated(runtime: IntakeToolRuntime, name: str, args: dict) -> None:
    """Refuse an exact repeat of an earlier call, echoing its result.

    Weak models love to re-read the same list instead of deciding. The
    previous answer is handed back so the refusal is not a dead end.
    """
    messages = runtime.state["messages"]
    normalized = {key: str(value) for key, value in args.items()}
    results = {m.tool_call_id: m.text for m in messages if isinstance(m, ToolMessage)}
    for message in messages:
        if not isinstance(message, AIMessage):
            continue
        for call in message.tool_calls:
            if call["id"] == runtime.tool_call_id or call["name"] != name:
                continue
            if {key: str(value) for key, value in call["args"].items()} == normalized:
                previous = results.get(call["id"], "(result lost)")
                raise ToolRejection(
                    f"You already called {name} with these exact arguments. Its result was:\n{previous}\n"
                    "Do not call it again — decide what to do with the information you have."
                )


@tool
async def get_service_catalog(runtime: IntakeToolRuntime) -> str:
    """Re-read the list of services available to the requester's organization.

    The same list is already given to you at the start of this session. Call
    this only if you need to check it again after a refused classification —
    never as your first action.
    """
    ctx = runtime.context
    _reject_if_repeated(runtime, "get_service_catalog", {})
    services = await ctx.catalog_repo.find_services(bind_oql(ctx.intake.classify_service_oql, ctx.ticket.model_dump()))
    return f"Services available to this organization:\n{format_options(services)}"


@tool
async def get_subcategories(service_id: int, runtime: IntakeToolRuntime) -> str:
    """List the subcategories of one service, with the descriptions that say
    what a request of that kind must contain.

    Call this once you know which service the ticket belongs to. Read the
    description of the subcategory you pick: it is the checklist you evaluate
    the ticket against.

    Args:
        service_id: numeric ID of the service, taken from the service catalog.
    """
    ctx = runtime.context
    _reject_if_repeated(runtime, "get_subcategories", {"service_id": service_id})
    subcategories = await ctx.catalog_repo.find_subcategories(
        bind_oql(ctx.intake.classify_subcategory_oql, {**ctx.ticket.model_dump(), "service_id": str(service_id)})
    )
    if not subcategories:
        raise ToolRejection(
            f"Service {service_id} has no subcategories available for this ticket. "
            "Pick a different service, or ask the requester what they need if you cannot tell."
        )
    return f"Subcategories of service {service_id}:\n{format_options(subcategories)}"


@tool
async def set_classification(service_id: int, subcategory_id: int, runtime: IntakeToolRuntime) -> str:
    """Set the service and the subcategory of the ticket.

    Call this as soon as you are confident about both, and before you decide
    whether the ticket needs a clarifying question — the subcategory
    description defines what "complete" means for this ticket.

    Args:
        service_id: numeric ID of the service, from the service catalog.
        subcategory_id: numeric ID of a subcategory belonging to that service.
    """
    ctx = runtime.context
    ticket = ctx.ticket
    # Read the catalog fresh: the model may be working from a stale list, and
    # this id comes from an LLM straight into an OQL query
    services = await ctx.catalog_repo.find_services(bind_oql(ctx.intake.classify_service_oql, ticket.model_dump()))
    valid_service_ids = {service.id for service in services}
    if str(service_id) not in valid_service_ids:
        raise ToolRejection(
            f"Service {service_id} is not available to this organization. "
            f"Valid service IDs: {', '.join(sorted(valid_service_ids)) or 'none'}. "
            "Pick one of them, or use post_public_question if the ticket does not tell you which."
        )

    subcategories = await ctx.catalog_repo.find_subcategories(
        bind_oql(ctx.intake.classify_subcategory_oql, {**ticket.model_dump(), "service_id": str(service_id)})
    )
    valid_subcategory_ids = {subcategory.id for subcategory in subcategories}
    if str(subcategory_id) not in valid_subcategory_ids:
        raise ToolRejection(
            f"Subcategory {subcategory_id} does not belong to service {service_id}. "
            f"Valid subcategory IDs for that service: {', '.join(sorted(valid_subcategory_ids)) or 'none'}. "
            "Pick one of them, or use post_public_question if the ticket does not tell you which."
        )

    if ticket.service_id == str(service_id) and ticket.subcategory_id == str(subcategory_id):
        return (
            f"No change: the ticket is already classified as service {service_id}, "
            f"subcategory {subcategory_id}.\n{_REMAINS}"
        )

    fields = {"service_id": str(service_id), "subcategory_id": str(subcategory_id)}
    await ctx.ticket_repo.set_fields(ticket, fields)
    # Keep the run's ticket snapshot in step with iTop — later tools read it
    ticket.service_id = fields["service_id"]
    ticket.subcategory_id = fields["subcategory_id"]
    logger.info(f"{ticket.label}: classified service_id={service_id} subcategory_id={subcategory_id}")

    subcategory = next(item for item in subcategories if item.id == str(subcategory_id))
    description = subcategory.description.strip() or "(no requirements described)"
    return (
        f"Saved: service {service_id}, subcategory {subcategory_id} ({subcategory.name}).\n"
        f"Requirements recorded for this subcategory:\n{description}\n{_REMAINS}"
    )


@tool
async def find_similar_resolved_tickets(runtime: IntakeToolRuntime) -> str:
    """Find tickets similar to this one that have already been solved.

    Call this once, before finish_handoff, and put every reference it returns
    into your note — an engineer who sees how a similar case was solved starts
    from an answer instead of from scratch. It takes no arguments: the search
    runs on this ticket's own title and description.

    What comes back is a list of references in the form [[Class:Id]]. Copy them
    into the note character for character. Never write a reference of your own:
    these are the only ones that exist, and anything else points at nothing.
    """
    ctx = runtime.context
    ticket = ctx.ticket
    # Guaranteed by `tools_for`, which withholds this tool otherwise
    assert ctx.similar is not None
    _reject_if_repeated(runtime, "find_similar_resolved_tickets", {})

    hits = await ctx.similar.find(
        f"{ticket.title}\n\n{html_to_markdown(ticket.description)}",
        filters={"status": ctx.intake.resolved_statuses},
        chunk_kinds=ctx.intake.similar_chunk_kinds,
        # Explicit, not the port's default: the index gets private log chunks in
        # TASK-013, and intake must not start quoting internal correspondence
        # without a change here.
        visibilities=["public"],
        exclude=(ticket.obj_class, int(ticket.id)),
        updated_after=datetime.now(UTC) - timedelta(days=ctx.intake.similar_max_age_days),
        min_score=ctx.intake.similar_min_score,
        candidates=ctx.intake.similar_candidates,
        top=ctx.intake.similar_top,
    )
    logger.info(f"{ticket.label}: similar resolved tickets found: {len(hits)}")
    if not hits:
        # Not a rejection: "nothing similar" is an answer, and a rejection
        # would send the model looking for another way to ask.
        return "No similar solved tickets were found. Write the handoff note without references."
    references = "\n".join(f"[[{hit.obj_class}:{hit.obj_id}]]" for hit in hits)
    return (
        "Solved tickets similar to this one, most similar first. "
        "Copy these references into your note exactly as written:\n" + references
    )


@tool
async def post_public_question(question: str, runtime: IntakeToolRuntime) -> str:
    """Ask the requester one clarifying message, visible to them in the portal.

    Use it when you cannot classify the ticket, or when the subcategory
    description requires information the ticket does not contain. Ask for
    everything you need in this one message — the session ends here and the
    requester's answer arrives as a new event. Never mention services,
    subcategories, IDs or anything else internal.

    Args:
        question: the message for the requester, plain text, in the language
            of the ticket.
    """
    ctx = runtime.context
    ticket = ctx.ticket
    if not question.strip():
        raise ToolRejection("The question is empty. Write the actual text you want the requester to read.")

    # The counter is chosen by code, not by the model: leaving it to the model
    # would let it pick the budget it has not spent yet
    classifying = not ticket.has_subcategory
    state = await ctx.state_manager.get(ticket.label)
    cfg = ctx.intake

    if classifying and state.classify_rounds >= cfg.max_classify_rounds:
        # Rounds spent: hand an unclassifiable ticket to a human instead of
        # asking again.
        # Returned as success, not as a rejection: the ticket *is* finished,
        # and a rejection would leave the loop running — free to call
        # finish_handoff and write a second note over this one.
        logger.info(f"{ticket.label}: classify rounds exhausted, posting fallback note")
        await ctx.ticket_repo.append_private_log(ticket, cfg.classify_fallback_note)
        await ctx.state_manager.mark_done(ticket.label)
        return (
            "The requester has already been asked about this twice and the ticket is still unclassified, "
            "so it has been handed to a human instead. Your session is over."
        )
    if not classifying and state.rounds >= cfg.max_rounds:
        raise ToolRejection(
            "The requester has already been asked twice. Do not ask again — "
            "call finish_handoff with whatever information the ticket already has."
        )

    await ctx.ticket_repo.append_public_log(ticket, question)
    if classifying:
        await ctx.state_manager.increment_classify_rounds(ticket.label)
    else:
        await ctx.state_manager.increment_rounds(ticket.label)
    logger.info(f"{ticket.label}: posted a public question (classify={classifying})")
    return "The question has been posted. Your session is over — stop here."


@tool
async def finish_handoff(note: str, runtime: IntakeToolRuntime) -> str:
    """Write the handoff note for the engineer and finish the ticket.

    The last thing you do. Use it when the ticket has everything the
    subcategory description requires, or when you have already asked the
    requester as many times as you are allowed to. The note is internal — the
    requester never sees it.

    Args:
        note: 2-4 sentences for the engineer, plain text, in the language of
            the ticket.
    """
    ctx = runtime.context
    ticket = ctx.ticket
    if not note.strip():
        raise ToolRejection("The note is empty. Summarize the ticket for the engineer in 2-4 sentences.")

    await ctx.ticket_repo.append_private_log(ticket, note)
    await ctx.state_manager.mark_done(ticket.label)
    logger.info(f"{ticket.label}: handoff note posted, marked done")
    return "The handoff note has been posted and the ticket is ready for the engineer. Your session is over."


_CLASSIFICATION_TOOLS: list[BaseTool] = [get_service_catalog, get_subcategories, set_classification]
_HANDOFF_TOOLS: list[BaseTool] = [post_public_question, finish_handoff]

TOOLS: list[BaseTool] = [*_CLASSIFICATION_TOOLS, *_HANDOFF_TOOLS]


def tools_for(ticket: Ticket, *, similar: bool = False) -> list[BaseTool]:
    """The tools this run may use — classification ones only while it is needed.

    An already classified ticket must not be classified again. Left to its
    own judgement the agent re-runs the whole first stage on every webhook:
    it re-reads the subcategory list and re-sets the same values, paying two
    model calls and two iTop round trips for nothing — and occasionally
    proposing a *different* subcategory, which would overwrite a correct
    classification with a guess made from a longer conversation.

    Taking the tools away enforces the rule rather than requesting it: the
    model cannot call what it was never given. The subcategory description —
    the checklist the ticket is judged against — is in the prompt either way.

    `similar` says the deployment can actually search (a vector store, an
    embeddings endpoint, indexing switched on). The same reasoning applies:
    a tool that would answer "not configured" every time is better absent,
    and the prompt already tells the model to use only what it was given.
    """
    tools = _HANDOFF_TOOLS if ticket.has_service and ticket.has_subcategory else TOOLS
    # Useful in both sets: whichever way the run goes, it may end in a note
    return [*tools, find_similar_resolved_tickets] if similar else list(tools)
