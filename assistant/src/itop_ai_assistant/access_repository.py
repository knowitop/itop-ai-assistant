"""Adapter over iTop's own access-scoping data — not an authorization model.

The one fact read here (a principal's "Allowed Organizations") feeds the
pre-filter of [[ADR-003-rights-late-binding]] (TASK-015, R4): a hint that
narrows candidates before the walk, never the authority on what is visible.
`SimilarSearch._keep_resolvable` (`vector/search.py`), run under the same
principal's own token, remains the only place that decides that.
"""

from itop_ai_assistant.itop_client import Itop

_CLASS = "User"
_PROJECTION = ["allowed_org_list"]


class AccessRepository:
    """Reads the effective principal's iTop-side organization scope."""

    def __init__(self, itop: Itop):
        self._itop = itop

    async def allowed_org_ids(self) -> list[str] | None:
        """Organizations the current principal may access, or `None` for "all".

        `:current_contact_id` resolves per request, same placeholder
        `ItopProvider.ai_person_name()` uses on `Person` — here it is joined
        through `User.contactid` because the linked set lives on the account,
        not the contact. iTop's own convention is that an empty "Allowed
        Organizations" list means every organization is allowed, not zero
        (`dev-docs/reference/itop-api.md`); this method normalizes that to
        `None` so a caller can pass the result straight into `filters` under
        `ChunkStore.search()`'s own convention for "unrestricted" — an absent
        key, never an empty list under a present one
        ([[ADR-017-generic-filter-contract]]).
        """
        row = await self._itop.schema(_CLASS).find_one(
            {"contactid": ("=", ":current_contact_id")}, projection=_PROJECTION
        )
        if row is None:
            return None
        orgs = row.get("allowed_org_list") or []
        ids = [str(entry["allowed_org_id"]) for entry in orgs]
        return ids or None
