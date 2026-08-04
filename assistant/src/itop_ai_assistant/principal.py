"""Who a run acts as when it talks to iTop.

Not an authorization model: iTop is the authority, and a delegated token is
both the credential and the permission set — a request made with it returns
what its owner may see and nothing else. There is deliberately no `scopes`,
no `roles` and no `expires_at` here; any of those would be a second access
model, diverging from the real one the moment they disagree.

The token is a secret and never leaves this object: `label` is what reaches
the run journal and the change comment, and it names an identity without
quoting a credential.
"""

from dataclasses import dataclass
from typing import Literal

from itop_ai_assistant.itop_client import ItopAuth

PrincipalKind = Literal["service", "delegated"]


@dataclass(frozen=True, repr=False)
class Principal:
    """The identity of one run.

    `auth is None` means "the connection's own credentials" — the service
    account configured in section `itop`. That is not a placeholder: it is what
    keeps the service principal free of any secret of its own, so nothing new
    has to be stored for a run to act as the assistant.
    """

    kind: PrincipalKind
    label: str
    auth: ItopAuth | None = None
    # Display name of the human this run acts for, for the change comment.
    on_behalf_of: str | None = None

    @classmethod
    def service(cls) -> "Principal":
        return SERVICE

    @classmethod
    def delegated(cls, token: str, *, login: str, name: str) -> "Principal":
        """A run acting as a person, with the iTop token they sent us.

        The token is not stored anywhere: it arrives with the request that
        needs it and lives as long as the run does (see `dev-docs/architecture/platform.md`
        §3.5).
        """
        return cls(kind="delegated", label=f"engineer:{login}", auth=ItopAuth(token=token), on_behalf_of=name)

    def __repr__(self) -> str:
        # object.__str__ delegates here, so this one method also covers
        # f-strings, %-formatting and the locals a traceback prints.
        return f"Principal({self.label})"


SERVICE = Principal(kind="service", label="service")
