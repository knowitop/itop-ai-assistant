"""What the installation remembers about itself: the id, the language, the dates."""

import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import fakeredis
from redis.exceptions import RedisError

from itop_ai_assistant.telemetry.install import InstallIdentity
from itop_ai_assistant.util.redis_keyspace import (
    TELEMETRY_INSTALL_FIRST_SEEN_FIELD,
    TELEMETRY_INSTALL_ID_FIELD,
    TELEMETRY_INSTALL_KEY,
    TELEMETRY_INSTALL_SETUP_DAY_FIELD,
)


class InstallIdentityTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        self.install = InstallIdentity(self.redis)


class TestInstallId(InstallIdentityTestCase):
    async def test_generated_once_and_stable_afterwards(self):
        first = await self.install.install_id()

        self.assertTrue(first)
        self.assertEqual(first, await self.install.install_id())

    async def test_two_replicas_agree_on_one_id(self):
        """The loser of the race must adopt the winner's id, not keep its own —
        otherwise one installation counts as two."""
        other = InstallIdentity(self.redis)

        self.assertEqual(await self.install.install_id(), await other.install_id())

    async def test_is_not_derived_from_anything(self):
        """R1: the id must not be a fingerprint of the deployment. Two
        installations sharing every setting still differ, which is only
        possible if nothing but randomness went into it."""
        elsewhere = InstallIdentity(fakeredis.aioredis.FakeRedis(decode_responses=True))

        self.assertNotEqual(await self.install.install_id(), await elsewhere.install_id())

    async def test_the_loser_of_the_race_adopts_the_winners_id(self):
        """`HSETNX` refused our value, so the winner's is the one this
        installation is already known by."""
        raced = InstallIdentity(
            AsyncMock(hget=AsyncMock(side_effect=[None, "written-first"]), hsetnx=AsyncMock(return_value=0))
        )

        self.assertEqual("written-first", await raced.install_id())

    async def test_a_key_that_went_away_does_not_become_an_id(self):
        """Nothing to re-read is not an id: the same non-answer from every
        installation in that state would collapse them into one."""
        vanished = InstallIdentity(
            AsyncMock(hget=AsyncMock(side_effect=[None, None]), hsetnx=AsyncMock(return_value=0))
        )

        self.assertRegex(await vanished.install_id(), r"^[0-9a-f]{32}$")

    async def test_a_failing_redis_is_not_swallowed(self):
        """No Redis, no document: the counters live there too, and R5 forbids
        the sender a way around that."""
        broken = InstallIdentity(AsyncMock(hget=AsyncMock(side_effect=RedisError("down"))))

        with self.assertRaises(RedisError):
            await broken.install_id()


class TestAdminLanguage(InstallIdentityTestCase):
    async def test_unknown_until_somebody_opens_the_admin_ui(self):
        self.assertIsNone(await self.install.language())

    async def test_remembers_the_last_language_seen(self):
        await self.install.remember_language("en")
        await self.install.remember_language("ru")

        self.assertEqual("ru", await self.install.language())

    async def test_drops_the_region(self):
        await self.install.remember_language("ru-RU")

        self.assertEqual("ru", await self.install.language())

    async def test_ignores_anything_that_is_not_a_language_tag(self):
        for value in (None, "", "../../etc/passwd", "ООО Ромашка", "en_US_POSIX"):
            with self.subTest(value=value):
                await self.install.remember_language(value)

                self.assertIsNone(await self.install.language())

    async def test_recording_survives_a_failing_redis(self):
        """An admin request must not fail because telemetry could not take a
        note — the same rule the activity counters follow."""
        broken = InstallIdentity(AsyncMock(hset=AsyncMock(side_effect=RedisError("down"))))

        await broken.remember_language("ru")

    async def test_the_language_does_not_disturb_the_id(self):
        install_id = await self.install.install_id()
        await self.install.remember_language("ru")

        self.assertEqual(install_id, await self.redis.hget(TELEMETRY_INSTALL_KEY, TELEMETRY_INSTALL_ID_FIELD))


class TestTheDatesTheSenderChecks(InstallIdentityTestCase):
    """Both fields are reachable by hand — a restore, a support session — and
    the sender guards only against `RedisError`. A value it cannot parse would
    raise on every hourly tick and stop telemetry for the life of the
    installation, with nothing in the log but a tick that failed."""

    async def _store(self, field: str, value: str) -> None:
        await self.redis.hset(TELEMETRY_INSTALL_KEY, field, value)

    async def test_first_seen_is_recorded_once_and_kept(self):
        first = await self.install.first_seen()

        self.assertEqual(first, await InstallIdentity(self.redis).first_seen())

    async def test_a_first_seen_nobody_can_read_is_replaced_not_raised_over(self):
        await self._store(TELEMETRY_INSTALL_FIRST_SEEN_FIELD, "yesterday-ish")

        moment = await self.install.first_seen()

        self.assertGreater(moment, datetime.now(UTC) - timedelta(minutes=1))
        # Healed in place, so the next tick parses it instead of warning again.
        self.assertEqual(moment, await self.install.first_seen())

    async def test_a_first_seen_without_a_zone_is_read_as_utc(self):
        """Not strictness for its own sake: the sender subtracts this from an
        aware `now`, and a naive value raises `TypeError` there."""
        await self._store(TELEMETRY_INSTALL_FIRST_SEEN_FIELD, "2026-08-04T10:00:00")

        self.assertEqual(datetime(2026, 8, 4, 10, 0, tzinfo=UTC), await self.install.first_seen())

    async def test_a_setup_day_nobody_can_read_costs_one_document_not_all_of_them(self):
        await self._store(TELEMETRY_INSTALL_SETUP_DAY_FIELD, "2026-8-4")

        self.assertIsNone(await self.install.setup_day())
