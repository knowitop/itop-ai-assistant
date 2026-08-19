"""Initial messages for an intake run: stable content first, volatile last.

    [0] SystemMessage  system.md          — identical for every ticket
    [1] HumanMessage   catalog_human.md   — stable within one organization
    [2] HumanMessage   ticket_human.md    — this ticket
    [3..]              the agent's own working loop

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


async def build_initial_messages(ctx: IntakeContext, prompts: IntakePrompts) -> list[BaseMessage]:
    ticket = ctx.ticket
    service_context = await build_service_context(ticket, ctx.catalog_repo)

    # An already classified ticket gets no catalog: it cannot be reclassified
    # (`tools_for` withholds the tools), and the list would otherwise ride
    # along in the message history, paid for on every model call of the run.
    messages: list[BaseMessage] = [SystemMessage(content=prompts.system)]
    if not (ticket.has_service and ticket.has_subcategory):
        services = await ctx.catalog_repo.find_services(bind_oql(ctx.intake.classify_service_oql, ticket.model_dump()))
        catalog_text = PromptTemplate.from_template(prompts.catalog_human).format(services=format_options(services))
        messages.append(HumanMessage(content=catalog_text))

    ticket_text = PromptTemplate.from_template(prompts.ticket_human).format(
        caller_name=ticket.caller_name,
        title=ticket.title,
        description=html_to_markdown(ticket.description),
        conversation=build_conversation_xml(ticket.public_log, ctx.ai_name, ticket.caller_name),
        service_context=service_context,
    )
    messages.append(HumanMessage(content=ticket_text))
    return messages
