"""Setup API: runtime connection configuration — backend for the setup wizard.

Connection sections (itop, llm, security, mappings) are stored through
the same ConfigStore as module config (Redis overrides > env defaults), but
served by dedicated endpoints because secrets need special treatment:

- GET never returns secret values — only `secrets: {field: is_set}` flags;
- PATCH merges the body over the current *effective* config, so a field
  absent from the body keeps its value and an explicit null clears it.
"""

import asyncio
import logging
from dataclasses import asdict
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from langchain_core.tools import tool
from pydantic import BaseModel, ValidationError
from redis.exceptions import RedisError

from itop_ai_assistant.config import (
    DECLARABLE_KINDS,
    EmbeddingsConfig,
    ItopConfig,
    LlmConfig,
    MappingsConfig,
    PlatformConfig,
    SecurityConfig,
    TelemetryConfig,
    missing_setup,
)
from itop_ai_assistant.content_sources.registry import declared_org_fields
from itop_ai_assistant.core.api_deps import get_config_store, get_install
from itop_ai_assistant.core.deps import AppDeps, create_llm
from itop_ai_assistant.core.llm_providers import PROVIDERS, get_provider
from itop_ai_assistant.domain.schema import FieldKind, Role, is_singular, kind_for
from itop_ai_assistant.itop.connection import create_itop_client
from itop_ai_assistant.itop.provisioning import provision_itop
from itop_ai_assistant.pipelines.scheduler import PeriodicTasks
from itop_ai_assistant.settings.config_store import ConfigStore
from itop_ai_assistant.state.install import InstallIdentity
from itop_ai_assistant.telemetry.sender import SEND_TASK
from itop_ai_assistant.util.text import strip_thinking
from itop_ai_assistant.vector import VectorConfig, measure_embedding_dimension

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/setup")

SETUP_SECTIONS: dict[str, type[BaseModel]] = {
    "itop": ItopConfig,
    "llm": LlmConfig,
    "security": SecurityConfig,
    # Installation-wide switches; the dry run lives here (REQ-006)
    "platform": PlatformConfig,
    # Anonymous usage telemetry — one switch, no secrets (REQ-009 R5)
    "telemetry": TelemetryConfig,
    # How every object family maps onto the customer's datamodel — one
    # section, because a family is a declaration and not a class (ADR-034).
    "mappings": MappingsConfig,
    # Vector store (optional infrastructure — not part of missing_setup)
    "embeddings": EmbeddingsConfig,
    "vector": VectorConfig,
}

#: The sections `missing_setup` reads. Editing one of them can be the moment
#: the wizard finishes, which is the moment telemetry is first allowed to send
#: (REQ-009 R6, `telemetry/sender.py`).
_SETUP_GATE_SECTIONS = frozenset({"itop", "llm"})

_TEST_TIMEOUT = 30.0  # seconds; keeps connection tests from hanging the wizard
_PROVISION_TIMEOUT = 60.0  # seconds; provisioning makes ~10 sequential iTop requests


def _model_or_404(section: str) -> type[BaseModel]:
    model = SETUP_SECTIONS.get(section)
    if model is None:
        raise HTTPException(
            status_code=404, detail=f"Unknown setup section: {section}. Known: {sorted(SETUP_SECTIONS)}"
        )
    return model


def _masked(cfg: BaseModel) -> dict:
    """Section values safe to return to the UI: secrets replaced by is-set flags."""
    values = cfg.model_dump()
    secrets_state = {}
    for field in getattr(type(cfg), "SECRET_FIELDS", frozenset()):
        secrets_state[field] = bool(values.pop(field))
    return {"values": values, "secrets": secrets_state}


async def _merged_with_current(
    config_store: ConfigStore, section: str, model: type[BaseModel], body: dict[str, Any]
) -> dict:
    """Body merged over the current effective config.

    Absent field = keep current value (secrets survive UI round-trips),
    explicit null = clear.
    """
    current = await config_store.get(section, model)
    return {**current.model_dump(), **body}


async def _check_names_the_code_owns(config_store: ConfigStore, section: str, values: dict[str, Any]) -> None:
    """Cross-checks a config model cannot make on its own.

    A section validates its own shape; whether a name in it is a field of the
    family is another matter, and for `acl_org_fields` it has to be answered
    here — against the schemas *this deployment* has, so a field an
    administrator declared grants access exactly like a built-in one. A name no source declares would not fail anything at sweep time —
    it resolves to no organization, the object's ACL comes out empty, and an
    empty ACL is *passed* by the pre-filter (ADR-033). The class would simply
    stop being pre-filtered, and nothing but the sweep's empty-ACL warning
    would say so.

    A family no source is registered for is skipped rather than refused, the
    same tolerance the sweep gives it: nothing indexes it, so nothing reads
    its ACL either.
    """
    if section != "vector":
        return
    cfg = VectorConfig.model_validate(values)
    mappings = await config_store.get("mappings", MappingsConfig)
    declared = declared_org_fields(mappings.schemas())
    for family, family_cfg in cfg.families.items():
        known = declared.get(family)
        if known is None:
            continue
        for obj_class, class_cfg in family_cfg.classes.items():
            unknown = [name for name in class_cfg.acl_org_fields if name not in known]
            if unknown:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"vector.families.{family}.classes.{obj_class}.acl_org_fields: "
                        f"{sorted(unknown)} — the {family!r} family has {sorted(known)}"
                    ),
                )


async def _install_id_or_none(install: InstallIdentity) -> str | None:
    """This installation's id, or `None` while Redis is unreachable.

    The only read in this handler that would otherwise raise: `ConfigStore`
    degrades to env defaults on `RedisError`, and `InstallIdentity` must not
    (the telemetry document may not be built from a half-known installation).
    So the guard belongs here — this endpoint is what the Setup screen renders
    and what the layout refetches on every navigation, and a missing-setup list
    is exactly what an administrator came for when Redis is the thing that is
    down.
    """
    try:
        return await install.install_id()
    except RedisError as e:
        logger.warning(f"Install state unavailable, setup status carries no installation id: {e}")
        return None


@router.get("/status")
async def setup_status(
    config_store: Annotated[ConfigStore, Depends(get_config_store)],
    install: Annotated[InstallIdentity, Depends(get_install)],
) -> dict:
    itop_cfg = await config_store.get("itop", ItopConfig)
    llm_cfg = await config_store.get("llm", LlmConfig)
    security_cfg = await config_store.get("security", SecurityConfig)
    embeddings_cfg = await config_store.get("embeddings", EmbeddingsConfig)
    platform_cfg = await config_store.get("platform", PlatformConfig)
    missing = missing_setup(itop_cfg, llm_cfg)
    return {
        "configured": not missing,
        "missing": missing,
        # Top-level, not just inside `sections`: the UI shows the dry run on
        # every screen (REQ-006 R6) and this endpoint is the one it already
        # refetches on every navigation.
        "dry_run": platform_cfg.dry_run,
        # Here for the same reason, and because it identifies the installation
        # rather than its telemetry (REQ-009 R1): the System screen shows it,
        # and doesn't need a request of its own for one string.
        "install_id": await _install_id_or_none(install),
        "sections": {
            "itop": _masked(itop_cfg),
            "llm": _masked(llm_cfg),
            "security": _masked(security_cfg),
            "embeddings": _masked(embeddings_cfg),
            "platform": _masked(platform_cfg),
        },
    }


@router.get("/llm-providers")
async def llm_providers() -> dict:
    """The LLM endpoints this build can talk to — the UI renders its form from this.

    `supports_forced_tool_choice: null` means the answer depends on the server
    behind the URL, and the UI must ask (see `llm_providers`).

    Declared before `/{section}`, which would otherwise swallow the path.
    """
    return {"providers": [asdict(provider) for provider in PROVIDERS.values()]}


@router.get("/mappings/fields")
async def get_mapping_fields(config_store: Annotated[ConfigStore, Depends(get_config_store)]) -> dict[str, list[dict]]:
    """Per family, the semantic fields the mapping form has a row for.

    A vocabulary endpoint rather than the section's JSON Schema: the section
    holds a dictionary now, so its schema describes the shape and not the
    fields, and the fields are what the form renders. Descriptions travel with
    them (ADR-025) — the form carries no list of its own, and a field added to
    a declaration appears without a UI change.

    The families as this deployment has them, so a field an administrator
    declared gets a row like any other, marked as theirs.
    """
    mappings = await config_store.get("mappings", MappingsConfig)
    return {
        name: [
            {
                "name": spec.name,
                "description": spec.description,
                "default": spec.source,
                "kind": spec.kind.value,
                "multi": spec.multi,
                "roles": sorted(role.value for role in spec.roles),
                "declared": spec.from_config,
            }
            for spec in schema.fields
        ]
        for name, schema in mappings.schemas().items()
    }


@router.get("/mappings/vocabulary")
async def get_mapping_vocabulary() -> dict:
    """What a field an administrator declares may be: the kinds, the roles, and
    the rules tying the two together.

    The form that builds a declaration must not keep its own copy of those
    rules, or the two drift the moment a role is added (ADR-025) — so what is
    *valid* comes from here, and only the words shown next to each identifier
    are the SPA's. A role whose kind is not declarable (a timestamp) is
    published all the same: it is a fact about the family's own fields, and the
    form derives from `requires_kind` that no declaration can carry it.

    Declared before `/{section}`, which would otherwise swallow the path.
    """
    return {
        "kinds": [{"name": kind.value, "declarable": kind in DECLARABLE_KINDS} for kind in FieldKind],
        "roles": [
            {"name": role.value, "requires_kind": kind_for(role).value, "singular": is_singular(role)} for role in Role
        ],
    }


@router.get("/{section}/schema")
async def get_section_schema(section: str) -> dict:
    """The section's JSON Schema — the mapping form is built from it.

    Same reason as `/config/{module}/schema` for modules: a form must not
    carry its own list of fields, or a new field would need a UI change
    (ADR-025). Values are not involved, so nothing here needs masking.
    """
    return _model_or_404(section).model_json_schema()


@router.get("/{section}")
async def get_section(section: str, config_store: Annotated[ConfigStore, Depends(get_config_store)]) -> dict:
    model = _model_or_404(section)
    cfg = await config_store.get(section, model)
    return _masked(cfg)


async def _setup_missing(config_store: ConfigStore) -> list[str]:
    itop_cfg = await config_store.get("itop", ItopConfig)
    llm_cfg = await config_store.get("llm", LlmConfig)
    return missing_setup(itop_cfg, llm_cfg)


async def _note_wizard_finished(request: Request) -> None:
    """Tell telemetry that the wizard has just been completed.

    The *event*, not the state — and the difference is the whole point
    (REQ-009 R6). "Setup is complete" is true one second after an upgraded
    installation restarts, so a first send keyed on the state would leave
    before anyone could have found the switch; keyed on the transition, it
    happens on the installation that has just walked through the wizard, and
    an upgrade waits for the ordinary daily cycle instead.

    Waking the loop only spares the first document the wait for the next tick;
    the tick decides everything, including whether telemetry is on at all.
    """
    deps: AppDeps = request.app.state.deps
    await deps.install.note_setup_complete()
    tasks: PeriodicTasks = request.app.state.tasks
    tasks.wake(SEND_TASK)


@router.patch("/{section}")
async def update_section(
    section: str,
    body: dict[str, Any],
    request: Request,
    config_store: Annotated[ConfigStore, Depends(get_config_store)],
) -> dict:
    model = _model_or_404(section)
    gates_setup = section in _SETUP_GATE_SECTIONS
    was_incomplete = bool(await _setup_missing(config_store)) if gates_setup else False
    values = await _merged_with_current(config_store, section, model, body)
    try:
        await _check_names_the_code_owns(config_store, section, values)
        cfg = await config_store.set(section, values, model)
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    logger.info(f"Setup section {section!r} updated via admin API")
    if was_incomplete and not await _setup_missing(config_store):
        try:
            await _note_wizard_finished(request)
        except Exception as e:
            # The section is saved by now, so anything raised here would
            # answer 500 for a write that succeeded — and the retry would find
            # setup already complete, so the transition, and with it the first
            # document, would be lost for good (REQ-009 R6). Losing it to a
            # log line is the smaller of the two.
            logger.warning(f"Telemetry: the finished wizard was not recorded: {e}")
    return _masked(cfg)


@router.delete("/{section}", status_code=204)
async def reset_section(
    section: str,
    config_store: Annotated[ConfigStore, Depends(get_config_store)],
    fields: Annotated[list[str] | None, Query()] = None,
) -> None:
    """Reset the section to env defaults — the whole of it, or only `fields`.

    A section split across several admin forms is reset by the form that owns
    the fields, so resetting one does not silently revert another's.
    """
    model = _model_or_404(section)
    if fields is not None:
        unknown = [f for f in fields if f not in model.model_fields]
        if unknown:
            raise HTTPException(status_code=422, detail=f"Unknown fields for section {section!r}: {unknown}")

    await config_store.reset(section, fields)
    logger.info(f"Setup section {section!r} reset to env defaults via admin API (fields={fields or 'all'})")


@router.post("/test-itop")
async def test_itop(
    config_store: Annotated[ConfigStore, Depends(get_config_store)], body: dict[str, Any] | None = None
) -> dict:
    """Probe the iTop connection: auth + REST profile + AI service account.

    Body fields override the stored config for this probe only — nothing is
    saved. Secrets absent from the body are taken from the stored config, so
    the UI can re-test without re-entering the password.
    """
    values = await _merged_with_current(config_store, "itop", ItopConfig, body or {})
    try:
        cfg = ItopConfig(**values)
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    if not cfg.url:
        return {"ok": False, "error": "No URL: set the iTop REST API URL first"}
    if not cfg.has_auth:
        return {"ok": False, "error": "No credentials: set user+pwd or token"}

    client = create_itop_client(cfg)
    try:
        # Resolves the Person behind the credentials — fails on bad auth or a
        # missing "REST Services User" profile, exactly what the wizard checks.
        person = await asyncio.wait_for(
            client.schema("Person").find_one({"id": ("=", ":current_contact_id")}),
            timeout=_TEST_TIMEOUT,
        )
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    finally:
        await client.aclose()
    if person is None:
        return {"ok": False, "error": "Authenticated, but no Person is linked to the account"}
    return {"ok": True, "ai_person": person.get("friendlyname")}


@router.post("/provision-itop")
async def provision_itop_endpoint(
    body: dict[str, Any], config_store: Annotated[ConfigStore, Depends(get_config_store)]
) -> dict:
    """Create the iTop-side triggers and webhooks (see itop_provisioning).

    Requires one-time admin credentials in the body (`user`+`pwd` or `token`)
    — they are used for these requests only and are never stored. `url`
    defaults to the saved itop section; the webhook token comes from the
    saved security section.
    """
    security = await config_store.get("security", SecurityConfig)
    if not security.webhook_token:
        return {"ok": False, "error": "Set the webhook token first (security section)"}
    backend_url = str(body.get("backend_url") or "").strip()
    if not backend_url:
        return {"ok": False, "error": "backend_url is required"}

    stored = await config_store.get("itop", ItopConfig)
    try:
        cfg = ItopConfig(
            url=str(body.get("url") or stored.url),
            api_version=stored.api_version,
            timeout=stored.timeout,
            user=body.get("user"),
            pwd=body.get("pwd"),
            token=body.get("token"),
        )
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    if not cfg.has_auth:
        return {"ok": False, "error": "No admin credentials: provide user+pwd or token"}

    client = create_itop_client(cfg)
    try:
        report = await asyncio.wait_for(
            provision_itop(client, backend_url, security.webhook_token), timeout=_PROVISION_TIMEOUT
        )
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    finally:
        await client.aclose()
    logger.info(f"iTop provisioning finished: {[(r['status'], r['name']) for r in report]}")
    return {"ok": True, "report": report}


@tool
def _probe_tool(text: str) -> str:
    """Echo the given text back. Used only to check that the model can call tools."""
    return text


@router.post("/test-llm")
async def test_llm(
    config_store: Annotated[ConfigStore, Depends(get_config_store)], body: dict[str, Any] | None = None
) -> dict:
    """Probe the LLM endpoint. Nothing is saved.

    Two questions, because the intake module needs both answers: does the
    endpoint respond at all, and can the model call a tool? Tool calling is a
    hard requirement — a model that answers in prose wastes every run — and
    finding that out in the wizard beats finding it out on a live ticket.
    Where the endpoint claims to accept a forced `tool_choice`, the second
    probe forces it, so the claim is verified rather than trusted.
    """
    values = await _merged_with_current(config_store, "llm", LlmConfig, body or {})
    try:
        cfg = LlmConfig(**values)
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    provider = get_provider(cfg.provider)
    if provider.base_url_mode == "required" and not cfg.base_url:
        return {"ok": False, "error": "No endpoint: set the LLM base URL first"}
    if provider.api_key_mode == "required" and not cfg.api_key:
        return {"ok": False, "error": f"No API key: {provider.label} requires one"}
    if not cfg.model:
        return {"ok": False, "error": "No model: set llm model first"}

    llm = create_llm(cfg)
    try:
        answer = await asyncio.wait_for(llm.ainvoke("Reply with a single word: OK"), timeout=_TEST_TIMEOUT)
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    text = strip_thinking(answer.content, tuple(cfg.think_tags)).strip()

    result: dict[str, Any] = {"ok": True, "provider": cfg.provider, "model": cfg.model, "response": text[:200]}
    forced = cfg.endpoint_forces_tool_choice
    bound = llm.bind_tools([_probe_tool], tool_choice="any") if forced else llm.bind_tools([_probe_tool])
    try:
        reply = await asyncio.wait_for(
            bound.ainvoke("Call the _probe_tool tool with the text: ping"), timeout=_TEST_TIMEOUT
        )
        result["tool_calling"] = bool(getattr(reply, "tool_calls", None))
        if forced:
            result["forced_tool_choice_ok"] = True
    except Exception as e:
        # A rejected tool_choice is the expected failure here (DeepSeek answers
        # HTTP 400) — report it as its own verdict, not as a dead endpoint
        result["tool_calling"] = False
        result["tool_error"] = f"{type(e).__name__}: {e}"[:500]
        if forced:
            result["forced_tool_choice_ok"] = False
    return result


@router.post("/test-embeddings")
async def test_embeddings(
    config_store: Annotated[ConfigStore, Depends(get_config_store)], body: dict[str, Any] | None = None
) -> dict:
    """Probe the embeddings endpoint with one text. Nothing is saved.

    Measures the endpoint's real vector dimension (`embed_raw` skips the
    config check) so the wizard can flag a wrong `dimension` value via
    `dimension_match` instead of failing opaquely later.
    """
    values = await _merged_with_current(config_store, "embeddings", EmbeddingsConfig, body or {})
    try:
        cfg = EmbeddingsConfig(**values)
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    if not cfg.base_url:
        return {"ok": False, "error": "No endpoint: set the embeddings base URL first"}
    if not cfg.model:
        return {"ok": False, "error": "No model: set embeddings model first"}

    try:
        dimension = await asyncio.wait_for(measure_embedding_dimension(cfg), timeout=_TEST_TIMEOUT)
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    return {
        "ok": True,
        "model": cfg.model,
        "dimension": dimension,
        "dimension_match": dimension == cfg.dimension,
    }
