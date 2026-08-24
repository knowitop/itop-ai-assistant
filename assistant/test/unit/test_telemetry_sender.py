"""When the daily document may leave, and when nothing at all may happen."""

import unittest
from datetime import UTC, date, datetime, timedelta
from unittest.mock import AsyncMock, patch

import fakeredis
from redis.exceptions import RedisError

from itop_ai_assistant.config import ItopConfig, LlmConfig, Settings, TelemetryConfig, get_settings
from itop_ai_assistant.settings.config_store import RedisConfigStore
from itop_ai_assistant.state.install import InstallIdentity
from itop_ai_assistant.telemetry.sender import TelemetrySender
from itop_ai_assistant.telemetry.telemetrydeck import TelemetryDeckSink
from itop_ai_assistant.util.redis_keyspace import (
    INSTALL_FIRST_SEEN_FIELD,
    INSTALL_KEY,
    INSTALL_SETUP_DAY_FIELD,
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
        # These cases are about *which day*, so the build gate is held open:
        # the test suite runs from a checkout, where nothing sends at all
        # (`TestWhichBuildMaySend`).
        release = patch("itop_ai_assistant.telemetry.sender.is_release_build", return_value=True)
        release.start()
        self.addCleanup(release.stop)

    async def _configure(self, *, enabled: bool = True, setup_complete: bool = True) -> None:
        await self.config_store.set("telemetry", {"enabled": enabled}, TelemetryConfig)
        if setup_complete:
            await self.config_store.set("itop", {"url": "http://itop/rest.php", "token": "tok"}, ItopConfig)
            await self.config_store.set("llm", {"base_url": "http://llm/v1", "model": "gpt-test"}, LlmConfig)

    async def _first_seen(self, ago: timedelta) -> None:
        await self.redis.hset(INSTALL_KEY, INSTALL_FIRST_SEEN_FIELD, (datetime.now(UTC) - ago).isoformat())

    async def _wizard_finished(self, day: date) -> None:
        await self.redis.hset(INSTALL_KEY, INSTALL_SETUP_DAY_FIELD, day.isoformat())

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

    async def test_a_day_that_could_not_be_built_is_not_burned(self):
        """The claim exists so that one day never reaches the receiver twice.
        A document that failed to assemble never reached it once, so spending
        the day on it would buy nothing — and `_day_due` offers yesterday and
        nothing older, so a day spent here is a day gone for good."""
        await self._configure()
        await self._first_seen(timedelta(days=5))
        self.builder.build.side_effect = [RedisError("down"), object()]

        await self._sender().tick()
        await self._sender().tick()

        self.assertEqual(1, len(self.sink.documents))

    async def test_a_day_the_receiver_refused_is_lost_not_queued(self):
        """R8: the claim is taken before the send and is not given back."""
        await self._configure()
        await self._first_seen(timedelta(days=5))
        refusing = _Sink(delivered=False)

        await self._sender(refusing).tick()
        await self._sender(refusing).tick()

        self.assertEqual(1, len(refusing.documents))


class TestWhichBuildMaySend(SenderTestCase):
    """A build we did not publish reports nothing (REQ-009 R5).

    With the switch on by default, every clone that finished the setup wizard
    would otherwise count as an installation — the one number the requirement
    exists to produce.
    """

    def _unpublished(self):
        return patch("itop_ai_assistant.telemetry.sender.is_release_build", return_value=False)

    async def test_a_checkout_sends_nothing_and_opens_nothing(self):
        await self._configure()
        await self._first_seen(timedelta(days=5))

        with self._unpublished(), patch("httpx.AsyncClient") as client:
            await TelemetrySender(self.config_store, self.builder, self.install, TelemetryDeckSink()).tick()

        client.assert_not_called()
        self.builder.build.assert_not_awaited()

    async def test_the_gate_comes_before_the_switch_is_even_read(self):
        """Nothing is read and nothing is claimed — the tick ends before Redis."""
        await self._configure()
        await self._first_seen(timedelta(days=5))

        with self._unpublished():
            await self._sender().tick()

        self.assertEqual([], self.sink.documents)
        self.assertEqual([], await self.redis.keys("install:telemetry-sent:*"))

    async def test_an_unpublished_build_sends_when_it_is_allowed_to(self):
        """How the stand — and a real server deployed from source, which is
        otherwise never counted — sends anything at all. Separate from the test
        mark: whether a build may send and whether its signal is a test one are
        different questions, and the stand answers yes to both."""
        await self._configure()
        await self._first_seen(timedelta(days=5))

        with self._unpublished():
            sender = TelemetrySender(
                self.config_store, self.builder, self.install, self.sink, allow_unpublished_build=True
            )
            await sender.tick()

        self.assertEqual(1, len(self.sink.documents))

    async def test_the_test_mark_alone_does_not_unlock_an_unpublished_build(self):
        """`TELEMETRY_TEST_MODE` marks what is sent and decides nothing about
        whether anything is."""
        await self._configure()
        await self._first_seen(timedelta(days=5))

        with self._unpublished():
            await TelemetrySender(
                self.config_store, self.builder, self.install, TelemetryDeckSink(test_mode=True)
            ).tick()

        self.assertEqual([], self.sink.documents)
