"""The vendor adapter: the shape of one signal, and when a failure is worth repeating."""

import json
import unittest
from datetime import date
from unittest.mock import AsyncMock, patch

import httpx

from itop_ai_assistant.state.counters import Counter
from itop_ai_assistant.telemetry.document import Build, Configuration, Environment, TelemetryDocument
from itop_ai_assistant.telemetry.telemetrydeck import TelemetryDeckSink

DAY = date(2026, 8, 20)
DOCUMENT = TelemetryDocument(
    install_id="0123456789abcdef",
    day=DAY,
    build=Build(version="1.2.3", commit=None, python_version="3.13", containerized=True),
    environment=Environment(qdrant=True, vector_available=False, admin_language=None, utc_offset_minutes=180),
    configuration=Configuration(
        dry_run=False,
        llm_provider="openai",
        llm_model="gpt-4o",
        settings={"intake_enabled": True, "intake_max_questions": 3},
    ),
    activity={counter.value: 0 for counter in Counter},
)


class SinkTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.requests: list[httpx.Request] = []
        # Retries are real seconds otherwise, and the point under test is how
        # many attempts happen, not how long they wait.
        self.sleep = self.enterContext(patch("itop_ai_assistant.telemetry.telemetrydeck.asyncio.sleep", AsyncMock()))

    def _sink(self, *responses: httpx.Response | Exception, test_mode: bool = False) -> TelemetryDeckSink:
        answers = list(responses)

        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            answer = answers.pop(0) if len(answers) > 1 else answers[0]
            if isinstance(answer, Exception):
                raise answer
            return answer

        return TelemetryDeckSink(test_mode=test_mode, transport=httpx.MockTransport(handler))


class TestSignalShape(SinkTestCase):
    async def test_the_installation_id_travels_as_itself(self):
        """Hashing a value generated from nothing protects nothing, and the
        receiver hashes `clientUser` again on arrival anyway (ADR-031). What
        the raw value buys is the only handle by which one installation's
        data can be found or deleted — the payload field."""
        await self._sink(httpx.Response(200)).send(DOCUMENT, first=False)

        signal = self.requests[0].read().decode()
        self.assertIn('"clientUser":"0123456789abcdef"', signal.replace(" ", ""))
        self.assertIn('"Install.id":"0123456789abcdef"', signal.replace(" ", ""))

    async def test_the_payload_is_flat_and_holds_primitives_only(self):
        """The receiver takes no nested objects and no arrays."""
        await self._sink(httpx.Response(200)).send(DOCUMENT, first=False)

        payload = json.loads(self.requests[0].read())[0]["payload"]
        for key, value in payload.items():
            self.assertIsInstance(key, str, key)
            self.assertIsInstance(value, str | int | float | bool, key)
        self.assertEqual(0, payload["Activity.runs_webhook"])
        self.assertTrue(payload["Config.Settings.intake_enabled"])
        self.assertEqual("2026-08-20", payload["Install.day"])

    async def test_a_field_with_no_value_is_left_out_rather_than_sent_empty(self):
        await self._sink(httpx.Response(200)).send(DOCUMENT, first=False)

        payload = json.loads(self.requests[0].read())[0]["payload"]
        self.assertNotIn("Build.commit", payload)
        self.assertNotIn("Environment.admin_language", payload)

    async def test_the_first_document_has_its_own_type(self):
        """It arrives once per installation and covers a partial day — both
        reasons to count it apart from the daily series."""
        await self._sink(httpx.Response(200)).send(DOCUMENT, first=True)
        await self._sink(httpx.Response(200)).send(DOCUMENT, first=False)

        types = [json.loads(request.read())[0]["type"] for request in self.requests]
        self.assertEqual(["Installation.setupCompleted", "Installation.daily"], types)

    async def test_a_stand_marks_its_signals_as_tests(self):
        await self._sink(httpx.Response(200), test_mode=True).send(DOCUMENT, first=False)

        self.assertTrue(json.loads(self.requests[0].read())[0]["isTestMode"])


class TestWhatIsWorthRepeating(SinkTestCase):
    async def test_a_receiver_error_is_tried_again(self):
        delivered = await self._sink(httpx.Response(503), httpx.Response(200)).send(DOCUMENT, first=False)

        self.assertTrue(delivered)
        self.assertEqual(2, len(self.requests))

    async def test_a_timeout_is_tried_again_and_then_given_up_on(self):
        delivered = await self._sink(httpx.ConnectTimeout("too slow")).send(DOCUMENT, first=False)

        self.assertFalse(delivered)
        self.assertEqual(3, len(self.requests))

    async def test_a_refused_signal_is_not_repeated(self):
        """A 4xx is a statement about our request — a wrong app id, a body
        they will not parse. Repeating it changes nothing."""
        delivered = await self._sink(httpx.Response(400, text="bad appID")).send(DOCUMENT, first=False)

        self.assertFalse(delivered)
        self.assertEqual(1, len(self.requests))
