"""Initial messages for an intake run: stable content first, volatile last.

    [0] SystemMessage  system.md + fragments — identical for every ticket
    [1] HumanMessage   catalog_human.md      — stable within one organization
    [2] HumanMessage   ticket_human.md       — this ticket
    [3..]                                      the agent's own working loop

The split between [1] and [2] is deliberate: prefix caching keys on the token
prefix, so an explicit boundary keeps the catalog part reusable across tickets.

The conversation is rendered as an XML block inside [2] rather than as real
Human/AI messages: in a tool-calling loop the `messages` array is the agent's
own draft, and foreign `AIMessage`s in it are indistinguishable from "my
previous turn". XML also keeps the channel and the author, which the message
roles cannot express.
"""

from xml.sax.saxutils import escape, quoteattr

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langchain_core.prompts import PromptTemplate

from itop_ai_assistant.domain.catalog import Service, ServiceSubcategory
from itop_ai_assistant.domain.ticket import LogEntry, Ticket
from itop_ai_assistant.repositories.catalog import CatalogRepository
from itop_ai_assistant.util.text import bind_oql, html_to_markdown

from .context import IntakeContext
from .domain import IntakeScope, closing_tools, needs_classification
from .prompts import IntakePrompts


def format_options(options: list[Service] | list[ServiceSubcategory]) -> str:
    """Render catalog entries as a flat id/name/description list for the model."""
    lines = []
    for opt in options:
        line = f"- ID {opt.id}: {opt.name}"
        desc = opt.description.strip()
        if desc:
            line += f" — {desc}"
        lines.append(line)
    return "\n".join(lines) if lines else "(no options available)"


def build_conversation_xml(entries: list[LogEntry], ai_name: str, caller_name: str) -> str:
    """Render the public log as an XML block.

    Escaping is mandatory: a requester writing "5 < 10 & rising" would
    otherwise break the block. `date` is not rendered — `LogEntry` has none.
    """
    lines = ["<conversation>"]
    for entry in entries:
        if entry.user_login == ai_name:
            role = "self"
        elif entry.user_login == caller_name:
            role = "requester"
        else:
            role = "agent"
        author = quoteattr(entry.user_login)
        lines.append(f'  <entry channel="public" author={author} role="{role}">{escape(entry.message)}</entry>')
    lines.append("</conversation>")
    return "\n".join(lines)


async def build_service_context(ticket: Ticket, catalog: CatalogRepository) -> str:
    """Describe the classification the ticket already carries.

    Matters on round 2+: without it the agent would re-read the subcategory
    list on every run just to learn what it picked last time.
    """
    parts = []
    if ticket.service_id is not None:
        service = await catalog.get_service(ticket.service_id)
        if service:
            parts.append(f"Service: {service.name}")
            if service.description:
                parts.append(f"Service description:\n{service.description}")
    if ticket.subcategory_id is not None:
        subcategory = await catalog.get_subcategory(ticket.subcategory_id)
        if subcategory:
            parts.append(f"Subcategory: {subcategory.name}")
            if subcategory.description:
                parts.append(f"Subcategory description:\n{subcategory.description}")

    if not parts:
        return "Not classified yet — neither service nor subcategory is set."
    return "\n".join(parts)


def format_session_scope(scope: IntakeScope, ticket: Ticket) -> str:
    """List what this run may do, and what ends it.

    Assembled in code rather than kept as a template fragment — the same call
    `build_service_context` makes: four sentences behind four conditions would
    be four files plus the code that joins them.

    Reads the same two rules `tools_for` does, `needs_classification`
    included, so the list and the tool set cannot disagree: a session told it
    may classify while the tools are withheld spends a turn looking for them.
    """
    actions = []
    if needs_classification(scope, ticket):
        actions.append("- Classify the ticket: set its service and its subcategory.")
    if scope.clarify:
        actions.append("- Ask the requester one clarifying question, and end the session with it.")
    if scope.similar:
        actions.append("- Look up solved tickets similar to this one, to quote in the note.")
    if scope.handoff_note:
        actions.append("- Write the handoff note for the engineer.")
    else:
        actions.append("- Finish the ticket. Nothing is written down: this deployment keeps no internal notes.")
    closing = " or ".join(closing_tools(scope))
    return "\n".join(actions) + f"\n\nThis session ends with exactly one call to {closing}, and nothing else."


def build_system_prompt(scope: IntakeScope, prompts: IntakePrompts) -> str:
    """The base fragment plus one fragment per action this deployment performs.

    An instruction for an action nobody switched on is the same mistake as a
    tool for it (ADR-012), one level down: the tool set is already assembled
    per run, and this leaves the text to match.

    Takes the scope and not the whole context on purpose — `system.md` is the
    cached prefix, and the scope is fixed for a deployment while the ticket is
    not. Selecting a fragment by what the ticket still needs would split the
    prefix into two families of runs for the sake of one caveat.
    """
    fragments = [prompts.system]
    if scope.classify:
        fragments.append(prompts.system_classify)
    if scope.clarify:
        fragments.append(prompts.system_clarify)
    if scope.handoff_note:
        fragments.append(prompts.system_handoff_note)
    if scope.similar:
        fragments.append(prompts.system_similar)
    # Trimmed rather than joined as-is: an override arrives from a textarea in
    # the admin UI and need not end with a newline.
    return "\n\n".join(fragment.strip() for fragment in fragments)


async def build_initial_messages(ctx: IntakeContext, prompts: IntakePrompts) -> list[BaseMessage]:
    ticket = ctx.ticket
    service_context = await build_service_context(ticket, ctx.catalog_repo)

    messages: list[BaseMessage] = [SystemMessage(content=build_system_prompt(ctx.scope, prompts))]

    # No catalog where classification cannot happen — an already classified
    # ticket, or a deployment that does not classify at all (`tools_for`
    # withholds the tools either way). The list would otherwise ride along in
    # the message history, paid for on every model call of the run.
    if needs_classification(ctx.scope, ticket):
        services = await ctx.catalog_repo.find_services(bind_oql(ctx.intake.classify_service_oql, ticket.model_dump()))
        catalog_text = PromptTemplate.from_template(prompts.catalog_human).format(services=format_options(services))
        messages.append(HumanMessage(content=catalog_text))

    ticket_text = PromptTemplate.from_template(prompts.ticket_human).format(
        caller_name=ticket.caller_name,
        title=ticket.title,
        description=html_to_markdown(ticket.description),
        conversation=build_conversation_xml(ticket.public_log, ctx.ai_name, ticket.caller_name),
        service_context=service_context,
        session_scope=format_session_scope(ctx.scope, ticket),
    )
    messages.append(HumanMessage(content=ticket_text))
    return messages
