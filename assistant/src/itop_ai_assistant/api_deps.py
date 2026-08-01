"""FastAPI dependencies shared by the entry points.

They live outside any single router because more than one entry point needs
them: the webhook and the synchronous request path both refuse to run until the
connections are configured, and both admin-token holders and webhook callers
reach the same effective `security` section.
"""

import logging
import secrets
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from itop_ai_assistant.config import ItopConfig, LlmConfig, SecurityConfig, missing_setup
from itop_ai_assistant.deps import AppDeps
from itop_ai_assistant.principal import Principal

logger = logging.getLogger(__name__)

# auto_error=False: a missing header must fall through to our own check —
# the API is open until an admin token is set (first-run mode)
_bearer = HTTPBearer(auto_error=False)


async def verify_webhook_token(request: Request, x_auth_token: Annotated[str | None, Header()] = None) -> None:
    deps: AppDeps = request.app.state.deps
    security = await deps.config_store.get("security", SecurityConfig)
    if security.webhook_token is None:
        return
    if x_auth_token is None or not secrets.compare_digest(x_auth_token, security.webhook_token):
        raise HTTPException(status_code=401, detail="Invalid or missing X-Auth-Token header")


async def verify_admin_token(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)] = None,
) -> None:
    deps: AppDeps = request.app.state.deps
    security = await deps.config_store.get("security", SecurityConfig)
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
    under. Neither needs a stored secret (`docs/architecture.md` §3.5).

    Called for its value rather than declared as a dependency: the routers need
    the principal itself, and a `Depends` would advertise a security scheme in
    the OpenAPI document before there is one to advertise. It becomes a
    dependency the day it has to answer 401 on a revoked token.
    """
    return Principal.service()


async def require_configured(request: Request) -> None:
    """Reject a run request until the connections are configured (env or setup API)."""
    deps: AppDeps = request.app.state.deps
    missing = missing_setup(
        await deps.config_store.get("itop", ItopConfig),
        await deps.config_store.get("llm", LlmConfig),
    )
    if missing:
        raise HTTPException(
            status_code=503,
            detail=f"Assistant is not configured: {'; '.join(missing)}. Complete setup via /api/setup/status.",
        )
