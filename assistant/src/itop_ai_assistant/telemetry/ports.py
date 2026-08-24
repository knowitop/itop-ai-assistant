"""The one thing the sender knows about the receiver: that it takes a document.

REQ-009 R9 asks for a vendor that can be replaced for the price of an adapter,
and this file is what makes that price hold. Nothing about a protocol, an
address, a payload shape or an account appears here — the sender
(`sender.py`) decides *whether* and *for which day*, the adapter
(`telemetrydeck.py`) decides everything else.

`bool` rather than a report of what went wrong: the caller has nothing to do
with the reason. A failed send changes no run, raises no alarm and is not
retried past the tick it happened in (R8), so "delivered / not delivered" is
the whole of what the port owes.
"""

from typing import Protocol

from itop_ai_assistant.telemetry.document import TelemetryDocument


class TelemetrySink(Protocol):
    """Whatever takes the daily document off this installation's hands."""

    async def send(self, document: TelemetryDocument, *, first: bool) -> bool:
        """Deliver the document. Never raises — a receiver is not an event.

        `first` marks the one document an installation sends on the day its
        wizard was finished (REQ-009 R6). It is ours, not the vendor's: that
        document covers a partial day, and an adapter that has any way of
        saying so should, if only to keep a half day out of the daily series.
        One that has not may ignore it.
        """
        ...
