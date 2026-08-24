"""When the daily document may leave, and when nothing at all may happen."""

import unittest
from datetime import UTC, date, datetime, timedelta
from unittest.mock import AsyncMock, patch

import fakeredis

from itop_ai_assistant.config import ItopConfig, LlmConfig, Settings, TelemetryConfig, get_settings
from itop_ai_assistant.settings.config_store import RedisConfigStore
from itop_ai_assistant.telemetry.install import InstallIdentity
from itop_ai_assistant.telemetry.sender import TelemetrySender
from itop_ai_assistant.telemetry.telemetrydeck import TelemetryDeckSink
from itop_ai_assistant.util.redis_keyspace import (
    TELEMETRY_INSTALL_FIRST_SEEN_FIELD,
    TELEMETRY_INSTALL_KEY,
    TELEMETRY_INSTALL_SETUP_DAY_FIELD,
)


class _Sink:
    """A receiver that records instead of connecting."""

    def __init__(self, delivered: bool = True) -> None:
        self.delivered = delivered
        self.documents: list = []
        self.first: list[bool] = []

    async def send(self, document, *, first: bool) -> bool:
        self.documents.append(document)
        self.first.append(first)
        return self.delivered


class SenderTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        get_settings.cache_clear()
        self.addCleanup(get_settings.cache_clear)
        self.redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        self.settings = Settings()
        self.config_store = RedisConfigStore(self.redis, self.settings)
        self.install = InstallIdentity(self.redis)
        self.builder = AsyncMock()
        self.sink = _Sink()
        self.today = datetime.now(UTC).date()

    async def _configure(self, *, enabled: bool = True, setup_complete: bool = True) -> None:
        await self.config_store.set("telemetry", {"enabled": enabled}, TelemetryConfig)
        if setup_complete:
            await self.config_store.set("itop", {"url": "http://itop/rest.php", "token": "tok"}, ItopConfig)
            await self.config_store.set("llm", {"base_url": "http://llm/v1", "model": "gpt-test"}, LlmConfig)

    async def _first_seen(self, ago: timedelta) -> None:
        await self.redis.hset(
            TELEMETRY_INSTALL_KEY, TELEMETRY_INSTALL_FIRST_SEEN_FIELD, (datetime.now(UTC) - ago).isoformat()
        )

    async def _wizard_finished(self, day: date) -> None:
        await self.redis.hset(TELEMETRY_INSTALL_KEY, TELEMETRY_INSTALL_SETUP_DAY_FIELD, day.isoformat())

    def _sender(self, sink=None) -> TelemetrySender:
        return TelemetrySender(self.config_store, self.builder, self.install, sink or self.sink)

    def _days_built(self) -> list[date]:
        return [call.args[0] for call in self.builder.build.await_args_list]


class TestWhenNothingMayHappen(SenderTestCase):
    async def test_switched_off_builds_no_client_at_all(self):
        """The requirement's own acceptance criterion, and the reason it is
        worded that way: Grafana's opt-out instance kept resolving the
        receiver's name, and only the people who closed outbound traffic
        found out."""
        await self._configure(enabled=False)
        await self._first_seen(timedelta(days=5))

        with patch("itop_ai_assistant.telemetry.telemetrydeck.httpx.AsyncClient") as client:
            await self._sender(TelemetryDeckSink()).tick()

        client.assert_not_called()
        self.builder.build.assert_not_awaited()

    async def test_an_unfinished_wizard_sends_nothing(self):
        await self._configure(setup_complete=False)
        await self._first_seen(timedelta(days=5))

        await self._sender().tick()

        self.assertEqual([], self.sink.documents)

    async def test_the_first_day_of_an_upgraded_installation_is_not_sent(self):
        """No wizard event to arm the first send, so R6's floor applies: an
        installation restarted an hour ago must not report before anyone
        could have found the switch."""
        await self._configure()
        await self._first_seen(timedelta(hours=1))

        await self._sender().tick()

        self.assertEqual([], self.sink.documents)

    async def test_redis_gone_is_not_an_error(self):
        await self._configure()
        await self.redis.aclose()

        await self._sender().tick()  # must not raise

        self.assertEqual([], self.sink.documents)


class TestWhichDay(SenderTestCase):
    async def test_the_finished_wizard_sends_today(self):
        await self._configure()
        await self._first_seen(timedelta(minutes=5))
        await self._wizard_finished(self.today)

        await self._sender().tick()

        self.assertEqual([self.today], self._days_built())
        # Its own signal type at the vendor: one per installation, ever, and a
        # partial day that must stay out of the daily series.
        self.assertEqual([True], self.sink.first)

    async def test_an_established_installation_sends_yesterday_whole(self):
        await self._configure()
        await self._first_seen(timedelta(days=5))

        await self._sender().tick()

        self.assertEqual([self.today - timedelta(days=1)], self._days_built())
        self.assertEqual([False], self.sink.first)

    async def test_the_day_after_the_wizard_falls_back_to_the_daily_cycle(self):
        await self._configure()
        await self._first_seen(timedelta(days=3))
        await self._wizard_finished(self.today - timedelta(days=2))

        await self._sender().tick()

        self.assertEqual([self.today - timedelta(days=1)], self._days_built())


class TestOneSendPerInstallation(SenderTestCase):
    async def test_a_second_tick_the_same_day_sends_nothing(self):
        await self._configure()
        await self._first_seen(timedelta(days=5))

        await self._sender().tick()
        await self._sender().tick()

        self.assertEqual(1, len(self.sink.documents))

    async def test_two_replicas_send_once_between_them(self):
        """Two replicas tick independently (`schedule/runner.py` says so in as
        many words). For tickets that is harmless; for counting installations
        it would double the day."""
        await self._configure()
        await self._first_seen(timedelta(days=5))

        await self._sender().tick()
        await TelemetrySender(self.config_store, self.builder, InstallIdentity(self.redis), self.sink).tick()

        self.assertEqual(1, len(self.sink.documents))

    async def test_a_day_the_receiver_refused_is_lost_not_queued(self):
        """R8: the claim is taken before the send and is not given back."""
        await self._configure()
        await self._first_seen(timedelta(days=5))
        refusing = _Sink(delivered=False)

        await self._sender(refusing).tick()
        await self._sender(refusing).tick()

        self.assertEqual(1, len(refusing.documents))
