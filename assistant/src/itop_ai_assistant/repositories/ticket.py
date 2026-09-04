"""The ticket family as a typed object.

A thin view over `ObjectRepository`: the reading, the projection and the write
counting are generic and live there. What is here is the one thing that is not
— `Ticket`, a static type, because `intake` writes `ticket.caller_name` and
`ticket.public_log` as identifiers and wants mypy to check them. A family that
lives only in the index needs no such class and gets none.
"""

from itop_ai_assistant.domain.object_view import ObjectView
from itop_ai_assistant.domain.ticket import Ticket
from itop_ai_assistant.repositories.object_repo import ObjectRepository
from itop_ai_assistant.state.counters import Counter

# The ticket log each write goes to, and what the installation counts it as.
# The distinction is the ticket family's: a question to the requester and a
# note between engineers are two different things to a customer, and one
# `add_item` to `ObjectRepository` (REQ-009 R3).
_PUBLIC_LOG = "public_log"
_PRIVATE_LOG = "private_log"


def to_ticket(view: ObjectView) -> Ticket:
    """The typed ticket over one object view.

    Every value is already normalized by kind, and an unmapped field is absent
    from the view — so the model's own default answers for it, and there is no
    per-field decision left to make here.
    """
    return Ticket(obj_class=view.obj_class, id=view.id, **view.values)


class TicketRepository:
    """Reads and writes tickets as `Ticket`, over the generic repository.

    Only what a business module needs: reading one ticket and writing to it.
    Sweeping a class page by page is not here — the vector sweep neither wants
    the typed model nor should reach a module's repository to get it.

    A fetch excludes the private log: intake never reads it, and asking iTop
    for it drags a case log across the wire on every webhook.
    """

    def __init__(self, objects: ObjectRepository):
        self._objects = objects

    def unmapped(self, obj_class: str, names: tuple[str, ...]) -> tuple[str, ...]:
        """Which of `names` this deployment does not map — what a module asks
        before it starts reading fields it cannot do without."""
        return self._objects.unmapped(obj_class, names)

    async def fetch(self, obj_class: str, ticket_id: str) -> Ticket | None:
        view = await self._objects.read(obj_class, ticket_id, exclude={_PRIVATE_LOG})
        return None if view is None else to_ticket(view)

    async def set_fields(self, ticket: Ticket, fields: dict[str, str]) -> None:
        """Update ticket attributes in iTop; `fields` is keyed by semantic names."""
        await self._objects.set_fields(ticket.identity, fields)

    async def append_public_log(self, ticket: Ticket, message: str) -> None:
        await self._objects.append_log(ticket.identity, _PUBLIC_LOG, message, counter=Counter.ITOP_PUBLIC_COMMENT)

    async def append_private_log(self, ticket: Ticket, message: str) -> None:
        await self._objects.append_log(ticket.identity, _PRIVATE_LOG, message, counter=Counter.ITOP_PRIVATE_NOTE)
