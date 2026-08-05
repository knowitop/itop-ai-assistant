from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True, repr=False)
class ItopAuth:
    """Credentials for one iTop request.

    iTop authenticates every request on its own — user and password as form
    fields, a token as the Auth-Token header — so credentials are a property of
    the request, not of the connection. That is what lets one client serve
    several identities (see `Itop.as_`).

    The repr is hand-written: `object.__str__` delegates to `__repr__`, so
    masking here covers f-strings, %-formatting and the locals shown in a
    traceback in one place.
    """

    user: Optional[str] = None
    pwd: Optional[str] = None
    token: Optional[str] = None

    def __repr__(self) -> str:
        pwd = "***" if self.pwd else None
        token = "***" if self.token else None
        return f"ItopAuth(user={self.user!r}, pwd={pwd}, token={token})"
