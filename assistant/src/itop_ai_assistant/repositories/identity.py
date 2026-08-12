"""Who the connection is, as iTop sees it.

One read, deliberately apart from `AccessRepository`: that one answers what a
principal may *see* (its organization scope), this one answers who a principal
*is*. They share the `:current_contact_id` idiom and nothing else.

This repository is built by `ItopConnection` over its own client and is not part
of a `RepositorySet` — a run must not be able to resolve this name under a
delegated token, because the intake loop guard compares it against the author of
the last public comment (`.claude/rules/core.md`).
"""

from itop_ai_assistant.itop_client import Itop

_CLASS = "Person"
_PROJECTION = ["friendlyname"]


class IdentityRepository:
    """Reads the display name of the Person linked to the current account."""

    def __init__(self, itop: Itop):
        self._itop = itop

    async def current_person_name(self) -> str:
        """Friendly name of the account's Person, never an empty string.

        A service account with no Person linked is a real, reachable
        misconfiguration — the setup wizard probes for it — so it raises here
        rather than handing the loop guard a name that matches nobody.
        """
        person = await self._itop.schema(_CLASS).find_one({"id": ("=", ":current_contact_id")}, projection=_PROJECTION)
        if person is None:
            raise ValueError("No Person is linked to the iTop service account")
        return str(person["friendlyname"])
