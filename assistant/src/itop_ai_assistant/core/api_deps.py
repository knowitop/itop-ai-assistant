"""FastAPI dependencies shared by the entry points.

They live outside any single router because more than one entry point needs
them: the webhook and the synchronous request path both refuse to run until the
connections are configured, and both admin-token holders and webhook callers
reach the same effective `security` section.

A provider that must return the whole container (`get_deps`) lives next to the
class itself, in `core/deps.py`, not here — it is only used by
`webhook`/`request`/`admin`. `vector/router.py` does not import from this
module at all (TASK-037): it reaches its own assembled subsystem through
`request.app.state.vector` instead, which is what makes the plain top-level
`AppDeps` import below safe — nothing that imports this file is itself
imported by `core/deps.py`.
"""

import logging
import secrets
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from itop_ai_assistant.config import ItopConfig, LlmConfig, SecurityConfig, missing_setup
from itop_ai_assistant.core.deps import AppDeps
from itop_ai_assistant.core.principal import Principal
from itop_ai_assistant.settings.config_store import ConfigStore
from itop_ai_assistant.settings.prompt_store import PromptStore
from itop_ai_assistant.state.journal import RunJournal

logger = logging.getLogger(__name__)

# auto_error=False: a missing header must fall through to our own check —
# the API is open until an admin token is set (first-run mode)
_bearer = HTTPBearer(auto_error=False)


def get_config_store(request: Request) -> ConfigStore:
    """The one `AppDeps` field every entry point reads config through.

    Narrow on purpose (`.claude/rules/core.md`): a function that only needs
    `config_store` says so in its signature instead of taking the whole
    container off `request.app.state`.
    """
    deps: AppDeps = request.app.state.deps
    return deps.config_store


def get_prompt_store(request: Request) -> PromptStore:
    deps: AppDeps = request.app.state.deps
    return deps.prompt_store


def get_journal(request: Request) -> RunJournal:
    deps: AppDeps = request.app.state.deps
    return deps.journal


async def remember_admin_language(request: Request) -> None:
    """Note the language the admin UI is speaking, for the telemetry document.

    Hung on the admin router as a whole rather than called from the two
    handlers that read `lang` today (REQ-009 R10). The rule "remember to
    record it" is one a third endpoint would break, and the failure would
    look like an installation whose administrators never picked a language —
    the same argument that put the activity counters in the iTop write layer
    instead of in each module.

    A request without `lang` touches Redis not at all, and a language that is
    not a language tag is dropped by `InstallIdentity` rather than stored.
    """
    lang = request.query_params.get("lang")
    if lang is None:
        return
    deps: AppDeps = request.app.state.deps
    await deps.install.remember_language(lang)


async def verify_webhook_token(
    config_store: Annotated[ConfigStore, Depends(get_config_store)],
    x_auth_token: Annotated[str | None, Header()] = None,
) -> None:
    security = await config_store.get("security", SecurityConfig)
    if security.webhook_token is None:
        return
    if x_auth_token is None or not secrets.compare_digest(x_auth_token, security.webhook_token):
        raise HTTPException(status_code=401, detail="Invalid or missing X-Auth-Token header")


async def verify_admin_token(
    config_store: Annotated[ConfigStore, Depends(get_config_store)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)] = None,
) -> None:
    security = await config_store.get("security", SecurityConfig)
    if security.admin_token is None:
        # First-run mode: the API stays open until the wizard sets a token
        return
    if credentials is None or not secrets.compare_digest(credentials.credentials, security.admin_token):
        raise HTTPException(
            status_code=401,
            detail="Invalid or missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )


async def resolve_principal(request: Request) -> Principal:
    """Who this call acts as.

    Today: always the service account from section `itop`. This is the one
    function that changes when identity arrives, and both cases are the same
    mechanism — the engineer console sending a personal iTop token in a header
    on every call, and a webhook carrying the account it must be processed
    under. Neither needs a stored secret (`dev-docs/architecture/platform.md` §3.5).

    Called for its value rather than declared as a dependency: the routers need
    the principal itself, and a `Depends` would advertise a security scheme in
    the OpenAPI document before there is one to advertise. It becomes a
    dependency the day it has to answer 401 on a revoked token.
    """
    return Principal.service()


async def require_configured(config_store: Annotated[ConfigStore, Depends(get_config_store)]) -> None:
    """Reject a run request until the connections are configured (env or setup API)."""
    missing = missing_setup(
        await config_store.get("itop", ItopConfig),
        await config_store.get("llm", LlmConfig),
    )
    if missing:
        raise HTTPException(
            status_code=503,
            detail=f"Assistant is not configured: {'; '.join(missing)}. Complete setup via /api/setup/status.",
        )
