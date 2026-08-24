"""The one file that names the receiver, its address and its format.

Same device as `core/tracing_otel.py`, and the same reason: who takes the data
and in what shape is a detail, not architecture. REQ-009 R9 exists because
this choice is considered reversible — Sentry and PostHog cut Russia off by IP
after the fact and without warning (ADR-031) — so replacing the vendor has to
cost this file and nothing else. Everything above it speaks `TelemetrySink`.

Three things about the mapping are worth reading before changing it.

**The installation id travels as itself.** Salting and hashing it would
protect nothing: the id is generated from nothing (REQ-009 R1), so a hash of
it is the same random value in a different alphabet. The receiver salts and
hashes `clientUser` again on arrival — deliberately, so that nobody, us
included, can reverse it — which means the stored identity is a double hash
whatever we send, and also means an installation cannot be found in the
dashboard by its `clientUser` at all. The handle that does work is the
ordinary payload field, and that is what answers "delete my data" and what
lets an administrator connect an issue to what we see. A salt would have
bought none of it, and would have added a constant that quietly doubles the
installation count on the day somebody regenerates it.

**The signal is not backdated.** A document describes yesterday and is sent
today, so the receiver's own date axis runs a day ahead of the activity it
shows. The vendor does have a field for moving a timestamp back, but it means
"when this happened" and exists for signals a client queued locally — which is
the one thing R8 forbids us to do. Which period the numbers cover is a
question our document answers itself, in `Install.day`, and that is the field
to group by. The constant one-day offset is the price and is named here so
that a reader of the dashboard meets it as a decision rather than a bug.

**The payload is flat, and `None` is left out.** The receiver takes primitives
only — no nested objects, no arrays — and stores everything as a string, so a
key whose value is null would arrive as the word for nothing. Absence says the
same thing and reads better in a dashboard. The vendor's naming convention
(`Scope.SubScope.key`, `TelemetryDeck.` reserved) is theirs and lives here;
our document's own field names travel unchanged inside it.
"""

import asyncio
import logging
from typing import Any

import httpx

from itop_ai_assistant.telemetry.document import TelemetryDocument
from itop_ai_assistant.telemetry.ports import TelemetrySink

logger = logging.getLogger(__name__)

#: Ingest API v2. The namespace belongs to our organization and the app id to
#: our app; neither is a secret — analytics vendors ship both inside client
#: applications — so both travel in the image and neither has a config field
#: an administrator could point at somebody else's receiver (REQ-009 R5).
_INGEST_URL = "https://nom.telemetrydeck.com/v2/namespace/{namespace}/"
_NAMESPACE = "com.knowitop"
_APP_ID = "03AEB41D-912A-4BF5-B39F-DC4272DC5505"

#: Two types, one document. Not two questions — R2 still sends one aggregate a
#: day — but two kinds of day, and the receiver counts by type. The setup one
#: arrives once in an installation's life, so counting those signals answers
#: "how many installations finished the wizard" directly; and because that
#: document covers a partial day (REQ-009 R6), keeping it out of the daily
#: series is what stops half a day from skewing every average in it.
_SIGNAL_TYPE_DAILY = "Installation.daily"
_SIGNAL_TYPE_SETUP = "Installation.setupCompleted"

_TIMEOUT_SECONDS = 10.0

#: Waits between attempts, and therefore also the number of them. Retries live
#: inside one tick and never outside it — that is the line R8 draws: a receiver
#: that is briefly unreachable is worth three seconds, a receiver that is down
#: is worth a lost day and not a queue.
_PAUSES_SECONDS = (3.0, 15.0)


class TelemetryDeckSink(TelemetrySink):
    """Turns the document into one signal and posts it. Never raises."""

    def __init__(self, *, test_mode: bool = False, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._test_mode = test_mode
        self._transport = transport

    async def send(self, document: TelemetryDocument, *, first: bool) -> bool:
        """Post the document, or return False having said so quietly.

        The client is built here rather than in the constructor, and that is
        load-bearing: telemetry that is switched off must not resolve the
        receiver's name, let alone open a socket to it. This method is the
        only place either happens, and nothing reaches it while the switch is
        off (`sender.py`).
        """
        signal = self._signal(document, first=first)
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS, transport=self._transport) as client:
            for attempt in range(len(_PAUSES_SECONDS) + 1):
                delivered = await self._attempt(client, signal)
                if delivered is not None:
                    return delivered
                if attempt < len(_PAUSES_SECONDS):
                    await asyncio.sleep(_PAUSES_SECONDS[attempt])
        logger.info("telemetry: receiver unreachable, this day's document is dropped")
        return False

    async def _attempt(self, client: httpx.AsyncClient, signal: dict[str, Any]) -> bool | None:
        """True delivered, False refused for good, None worth another try.

        The split is the whole of the retry policy. A timeout, a broken
        connection, a 5xx or a 429 are states of the network and of the
        receiver, and a second attempt is the cheapest thing that can help.
        Any other 4xx is a statement about our request — a wrong app id, a
        body they will not parse — and repeating it changes nothing, which is
        also why it is the one case logged loudly enough to notice.
        """
        try:
            response = await client.post(_INGEST_URL.format(namespace=_NAMESPACE), json=[signal])
        except httpx.HTTPError as e:
            logger.info(f"telemetry: {type(e).__name__} talking to the receiver: {e}")
            return None
        if response.is_success:
            return True
        if response.status_code == 429 or response.status_code >= 500:
            logger.info(f"telemetry: receiver answered {response.status_code}")
            return None
        logger.warning(f"telemetry: receiver refused the signal: {response.status_code} {response.text[:200]}")
        return False

    def _signal(self, document: TelemetryDocument, *, first: bool) -> dict[str, Any]:
        return {
            "appID": _APP_ID,
            "clientUser": document.install_id,
            "type": _SIGNAL_TYPE_SETUP if first else _SIGNAL_TYPE_DAILY,
            "isTestMode": self._test_mode,
            "payload": _payload(document),
        }


def _payload(document: TelemetryDocument) -> dict[str, Any]:
    data = document.model_dump(mode="json")
    settings = data["configuration"].pop("settings")
    flat: dict[str, Any] = {"Install.id": data["install_id"], "Install.day": data["day"]}
    for field, scope in (("build", "Build"), ("environment", "Environment"), ("configuration", "Config")):
        flat.update({f"{scope}.{key}": value for key, value in data[field].items()})
    flat.update({f"Config.Settings.{key}": value for key, value in settings.items()})
    flat.update({f"Activity.{key}": value for key, value in data["activity"].items()})
    return {key: value for key, value in flat.items() if value is not None}
