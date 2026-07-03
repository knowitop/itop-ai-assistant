import unittest

from itop_client.exceptions import ItopError
from itop_provisioning import (
    APP_NAME,
    CREATED_TRIGGER_DESC,
    CREATED_WEBHOOK_NAME,
    UPDATED_WEBHOOK_NAME,
    provision_itop,
)

BACKEND_URL = "http://assistant:8000"
WEBHOOK_TOKEN = "wh-tok"


class FakeSchema:
    def __init__(self, itop: "FakeItop", name: str):
        self.itop = itop
        self.name = name

    async def find_one(self, query, projection=None):
        # Queries are exact-match tuples: {"name": ("=", value)}
        ((_field, (op, value)),) = query.items()
        assert op == "="
        return self.itop.existing.get((self.name, value))

    async def find(self, query=None, projection=None, limit="0", page="1"):
        # Used only as the class-existence probe.
        if self.name in self.itop.missing_classes:
            raise ItopError(100, f"Unknown class {self.name}")
        return []


class FakeItop:
    """Stands in for itop_client.Itop: lookups from a fixture, creates recorded."""

    def __init__(self, existing=None, missing_classes=()):
        self.existing = existing or {}  # (class, name/description) -> {"id": ...}
        self.missing_classes = set(missing_classes)
        self.creates: list[dict] = []
        self._next_id = 100

    def schema(self, name):
        return FakeSchema(self, name)

    async def request(self, data):
        assert data["operation"] == "core/create"
        self.creates.append(data)
        self._next_id += 1
        return [{"id": str(self._next_id)}]


ALL_EXISTING = {
    ("RemoteApplicationType", APP_NAME): {"id": "1"},
    ("RemoteApplicationConnection", APP_NAME): {"id": "2"},
    ("TriggerOnObjectCreate", CREATED_TRIGGER_DESC): {"id": "3"},
    ("ActionWebhook", CREATED_WEBHOOK_NAME): {"id": "4"},
    ("TriggerOnObjectUpdate", "UserRequest public log updated (iTop AI Assistant)"): {"id": "5"},
    ("TriggerOnObjectUpdate", "Incident public log updated (iTop AI Assistant)"): {"id": "6"},
    ("ActionWebhook", UPDATED_WEBHOOK_NAME): {"id": "7"},
}


class TestProvisionFreshSystem(unittest.IsolatedAsyncioTestCase):
    async def test_creates_all_objects_in_order(self):
        itop = FakeItop()

        report = await provision_itop(itop, BACKEND_URL, WEBHOOK_TOKEN)

        self.assertEqual([r["status"] for r in report], ["created"] * 7)
        self.assertEqual(
            [c["class"] for c in itop.creates],
            [
                "RemoteApplicationType",
                "RemoteApplicationConnection",
                "TriggerOnObjectCreate",
                "ActionWebhook",
                "TriggerOnObjectUpdate",
                "TriggerOnObjectUpdate",
                "ActionWebhook",
            ],
        )
        # Every id in the report comes from the create response
        self.assertTrue(all(r["id"] for r in report))

    async def test_created_object_fields(self):
        itop = FakeItop()

        await provision_itop(itop, BACKEND_URL, WEBHOOK_TOKEN)

        by_class = {}
        for create in itop.creates:
            by_class.setdefault(create["class"], []).append(create["fields"])

        connection = by_class["RemoteApplicationConnection"][0]
        self.assertEqual(connection["url"], BACKEND_URL)
        self.assertEqual(connection["remoteapplicationtype_id"], {"name": APP_NAME})

        create_trigger = by_class["TriggerOnObjectCreate"][0]
        self.assertEqual(create_trigger["target_class"], "Ticket")
        self.assertIn("finalclass IN ('UserRequest', 'Incident')", create_trigger["filter"])

        update_triggers = by_class["TriggerOnObjectUpdate"]
        self.assertEqual([t["target_class"] for t in update_triggers], ["UserRequest", "Incident"])
        for trigger in update_triggers:
            # REST/JSON excluded from the context — loop protection
            self.assertEqual(trigger["context"], "CRON, GUI:Console, GUI:Portal")
            self.assertEqual(trigger["target_attcodes"], "public_log")

        created_hook, updated_hook = by_class["ActionWebhook"]
        for hook in (created_hook, updated_hook):
            # The backend authenticates by X-Auth-Token, not Authorization: Bearer
            self.assertIn(f"X-Auth-Token: {WEBHOOK_TOKEN}", hook["headers"])
            self.assertEqual(hook["path"], "/webhook")
            self.assertEqual(hook["remoteapplicationconnection_id"], {"name": APP_NAME})
        self.assertIn('"event": "created"', created_hook["payload"])
        self.assertIn('"event": "user_commented"', updated_hook["payload"])
        self.assertEqual(len(updated_hook["trigger_list"]), 2)


class TestProvisionIdempotency(unittest.IsolatedAsyncioTestCase):
    async def test_existing_objects_are_not_recreated(self):
        itop = FakeItop(existing=dict(ALL_EXISTING))

        report = await provision_itop(itop, BACKEND_URL, WEBHOOK_TOKEN)

        self.assertEqual(itop.creates, [])
        self.assertEqual([r["status"] for r in report], ["exists"] * 7)
        self.assertEqual({r["id"] for r in report}, {"1", "2", "3", "4", "5", "6", "7"})


class TestProvisionMissingClass(unittest.IsolatedAsyncioTestCase):
    async def test_missing_incident_class_is_skipped(self):
        itop = FakeItop(missing_classes={"Incident"})

        report = await provision_itop(itop, BACKEND_URL, WEBHOOK_TOKEN)

        skipped = [r for r in report if r["status"] == "skipped"]
        self.assertEqual(len(skipped), 1)
        self.assertIn("Incident", skipped[0]["name"])

        update_triggers = [c for c in itop.creates if c["class"] == "TriggerOnObjectUpdate"]
        self.assertEqual(len(update_triggers), 1)
        self.assertEqual(update_triggers[0]["fields"]["target_class"], "UserRequest")

        updated_hook = next(
            c for c in itop.creates if c["class"] == "ActionWebhook" and "user_commented" in c["fields"]["payload"]
        )
        self.assertEqual(
            updated_hook["fields"]["trigger_list"],
            [{"trigger_id": {"description": "UserRequest public log updated (iTop AI Assistant)"}}],
        )

    async def test_both_classes_missing_skips_update_webhook(self):
        itop = FakeItop(missing_classes={"UserRequest", "Incident"})

        report = await provision_itop(itop, BACKEND_URL, WEBHOOK_TOKEN)

        self.assertEqual(len([r for r in report if r["status"] == "skipped"]), 2)
        classes = [c["class"] for c in itop.creates]
        self.assertNotIn("TriggerOnObjectUpdate", classes)
        self.assertEqual(classes.count("ActionWebhook"), 1)  # only the "created" hook


class TestProvisionErrors(unittest.IsolatedAsyncioTestCase):
    async def test_create_error_propagates(self):
        itop = FakeItop()

        async def failing_request(data):
            raise ItopError(1, "not enough rights")

        itop.request = failing_request

        with self.assertRaises(ItopError):
            await provision_itop(itop, BACKEND_URL, WEBHOOK_TOKEN)


if __name__ == "__main__":
    unittest.main()
