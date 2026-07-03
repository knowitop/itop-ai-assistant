"""One-shot provisioning of the iTop-side objects the assistant needs.

Creates the Remote Application Connection, the triggers and the webhooks that
make iTop call POST /webhook on ticket creation and public log updates. Runs
under one-time admin credentials (creating triggers requires admin rights);
the credentials are used for these requests only and are never stored.

Idempotent via find-or-create: objects are looked up by exact name (or
description for triggers) and existing ones are left untouched — a stale URL
or token shows up as "exists" in the report and is fixed manually in iTop.

Also runnable as a CLI (works without the assistant backend):

    PYTHONPATH=src uv run python -m itop_provisioning \\
        --itop-url http://itop/webservices/rest.php --user admin \\
        --backend-url http://assistant:8000 --webhook-token <token>
"""

import argparse
import asyncio
import getpass
import json
import sys
from typing import Any

from itop_client import Itop
from itop_client.exceptions import ItopError

APP_NAME = "iTop AI Assistant"
COMMENT = "iTop AI Assistant Setup Wizard"
TICKET_CLASSES = ("UserRequest", "Incident")

CREATED_TRIGGER_DESC = "UserRequest/Incident created (iTop AI Assistant)"
CREATED_WEBHOOK_NAME = "UserRequest/Incident created (iTop AI Assistant)"
UPDATED_WEBHOOK_NAME = "UserRequest/Incident public log updated (iTop AI Assistant)"

# Excludes REST/JSON so the assistant's own comments never re-trigger the
# webhook — the second line of defense next to the guard node.
UPDATE_TRIGGER_CONTEXT = "CRON, GUI:Console, GUI:Portal"


def _update_trigger_desc(obj_class: str) -> str:
    return f"{obj_class} public log updated (iTop AI Assistant)"


def _webhook_headers(webhook_token: str) -> str:
    return f"X-Auth-Token: {webhook_token}\r\nContent-type: application/json"


def _webhook_payload(event: str) -> str:
    return json.dumps({"id": "$this->id$", "class": "$this->finalclass$", "event": event})


async def provision_itop(client: Itop, backend_url: str, webhook_token: str) -> list[dict[str, Any]]:
    """Create the trigger/webhook objects in iTop; return a per-object report.

    Report items: {class, name, status: "created"|"exists"|"skipped", id}.
    The order matters — each object references the previous ones by name.
    Raises ItopError on the first failed request (bad credentials, missing
    rights, connectivity).
    """
    report: list[dict[str, Any]] = []

    async def ensure(obj_class: str, key_field: str, key_value: str, fields: dict[str, Any]) -> None:
        existing = await client.schema(obj_class).find_one({key_field: ("=", key_value)}, ["id"])
        if existing is not None:
            report.append({"class": obj_class, "name": key_value, "status": "exists", "id": existing["id"]})
            return
        created = await client.request(
            {
                "operation": "core/create",
                "comment": COMMENT,
                "class": obj_class,
                "output_fields": "id",
                "fields": fields,
            }
        )
        report.append({"class": obj_class, "name": key_value, "status": "created", "id": created[0]["id"]})

    await ensure("RemoteApplicationType", "name", APP_NAME, {"name": APP_NAME})
    await ensure(
        "RemoteApplicationConnection",
        "name",
        APP_NAME,
        {
            "name": APP_NAME,
            "url": backend_url,
            "environment": "3-production",
            "remoteapplicationtype_id": {"name": APP_NAME},
        },
    )
    await ensure(
        "TriggerOnObjectCreate",
        "description",
        CREATED_TRIGGER_DESC,
        {
            "description": CREATED_TRIGGER_DESC,
            "target_class": "Ticket",
            "filter": "SELECT Ticket WHERE finalclass IN ('UserRequest', 'Incident')",
            "subscription_policy": "force_all_channels",
        },
    )
    await ensure(
        "ActionWebhook",
        "name",
        CREATED_WEBHOOK_NAME,
        {
            "name": CREATED_WEBHOOK_NAME,
            "status": "enabled",
            "trigger_list": [{"trigger_id": {"description": CREATED_TRIGGER_DESC}}],
            "remoteapplicationconnection_id": {"name": APP_NAME},
            "method": "post",
            "path": "/webhook",
            "headers": _webhook_headers(webhook_token),
            "payload": _webhook_payload("created"),
        },
    )

    # The update triggers are per-class and only make sense for classes that
    # exist in this iTop's datamodel (a probe query fails on unknown classes;
    # auth problems would have failed the requests above already).
    present: list[str] = []
    for obj_class in TICKET_CLASSES:
        try:
            await client.schema(obj_class).find({}, ["id"], limit="1")
        except ItopError:
            report.append(
                {
                    "class": "TriggerOnObjectUpdate",
                    "name": _update_trigger_desc(obj_class),
                    "status": "skipped",
                    "id": None,
                }
            )
            continue
        present.append(obj_class)

    for obj_class in present:
        await ensure(
            "TriggerOnObjectUpdate",
            "description",
            _update_trigger_desc(obj_class),
            {
                "description": _update_trigger_desc(obj_class),
                "context": UPDATE_TRIGGER_CONTEXT,
                "target_class": obj_class,
                "target_attcodes": "public_log",
                "subscription_policy": "force_all_channels",
            },
        )

    if present:
        await ensure(
            "ActionWebhook",
            "name",
            UPDATED_WEBHOOK_NAME,
            {
                "name": UPDATED_WEBHOOK_NAME,
                "status": "enabled",
                "trigger_list": [
                    {"trigger_id": {"description": _update_trigger_desc(obj_class)}} for obj_class in present
                ],
                "remoteapplicationconnection_id": {"name": APP_NAME},
                "method": "post",
                "path": "/webhook",
                "headers": _webhook_headers(webhook_token),
                "payload": _webhook_payload("user_commented"),
            },
        )

    return report


async def _run_cli(args: argparse.Namespace) -> list[dict[str, Any]]:
    from config import ItopConfig
    from deps import create_itop_client

    cfg = ItopConfig(
        url=args.itop_url,
        api_version=args.api_version,
        user=args.user,
        pwd=args.pwd,
        token=args.token,
    )
    client = create_itop_client(cfg)
    try:
        return await provision_itop(client, args.backend_url, args.webhook_token)
    finally:
        await client.aclose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Create the iTop triggers and webhooks for the AI assistant (admin credentials, used once)."
    )
    parser.add_argument("--itop-url", required=True, help="iTop rest.php endpoint")
    parser.add_argument("--api-version", default="1.3", help="iTop REST API version (default: 1.3)")
    parser.add_argument("--user", help="iTop admin login (asks for the password if --pwd is omitted)")
    parser.add_argument("--pwd", help="iTop admin password")
    parser.add_argument("--token", help="iTop token auth — alternative to --user/--pwd")
    parser.add_argument("--backend-url", required=True, help="assistant URL as reachable from iTop")
    parser.add_argument("--webhook-token", required=True, help="shared secret expected by POST /webhook")
    args = parser.parse_args(argv)

    if args.user and not args.pwd:
        args.pwd = getpass.getpass("iTop admin password: ")
    if not (args.user and args.pwd) and not args.token:
        parser.error("provide --user/--pwd or --token")

    try:
        report = asyncio.run(_run_cli(args))
    except ItopError as e:
        print(f"iTop error: {e}", file=sys.stderr)
        return 1
    for item in report:
        print(f"{item['status']:>8}  {item['class']}  {item['name']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
