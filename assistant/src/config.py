from functools import lru_cache
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, SecretStr, model_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)

_ROOT = Path(__file__).parent.parent  # assistant/


_CLASSIFY_SERVICE_OQL = (
    "SELECT Service AS s"
    " JOIN lnkCustomerContractToService AS l1 ON l1.service_id=s.id"
    " JOIN CustomerContract AS cc ON l1.customercontract_id=cc.id"
    " WHERE cc.org_id = :this->org_id AND s.status != 'obsolete'"
)

_CLASSIFY_SUBCATEGORY_OQL = (
    "SELECT ServiceSubcategory"
    " WHERE service_id = :this->service_id"
    " AND (ISNULL(:this->request_type) OR request_type = :this->request_type)"
    " AND status != 'obsolete'"
)


class TicketFieldMap(BaseModel):
    """Semantic ticket field → iTop attribute code. None = attribute absent."""

    ref: str | None = "ref"
    title: str | None = "title"
    description: str | None = "description"
    status: str | None = "status"
    service_id: str | None = "service_id"
    subcategory_id: str | None = "servicesubcategory_id"
    caller_name: str | None = "caller_id_friendlyname"
    org_id: str | None = "org_id"
    request_type: str | None = "request_type"
    public_log: str | None = "public_log"
    private_log: str | None = "private_log"


class TicketMappingConfig(BaseModel):
    """How ticket semantics map onto the customer's iTop datamodel."""

    fields: TicketFieldMap = TicketFieldMap()
    # Per-class field overrides, e.g. a class without some attribute (None)
    # or with a renamed one. Merged over `fields` for that class.
    class_overrides: dict[str, dict[str, str | None]] = {
        "Incident": {"request_type": None},  # Incident has no request_type in stock iTop
    }
    # Process a ticket only while its status is in this list
    active_statuses: list[str] = ["new"]

    def for_class(self, obj_class: str) -> dict[str, str | None]:
        resolved = self.fields.model_dump()
        resolved.update(self.class_overrides.get(obj_class, {}))
        return resolved

    @model_validator(mode="after")
    def check_override_fields(self) -> "TicketMappingConfig":
        known = set(TicketFieldMap.model_fields)
        for obj_class, overrides in self.class_overrides.items():
            unknown = overrides.keys() - known
            if unknown:
                raise ValueError(
                    f"ticket_mapping.class_overrides[{obj_class!r}]: unknown fields {sorted(unknown)}, "
                    f"known: {sorted(known)}"
                )
        return self


class RuntimeSectionConfig(BaseModel):
    """Base for runtime-editable config sections holding secrets.

    Secrets are plain strings (not SecretStr) so the stored JSON round-trips;
    masking happens at the setup API boundary (SECRET_FIELDS). An empty
    string means "not set" — a common artifact of blank .env lines.
    """

    SECRET_FIELDS: ClassVar[frozenset[str]] = frozenset()

    @model_validator(mode="after")
    def blank_secrets_to_none(self) -> "RuntimeSectionConfig":
        for field in self.SECRET_FIELDS:
            if getattr(self, field) == "":
                setattr(self, field, None)
        return self


class ItopConfig(RuntimeSectionConfig):
    """iTop connection settings — runtime-editable section "itop"."""

    SECRET_FIELDS: ClassVar[frozenset[str]] = frozenset({"pwd", "token"})

    url: str = "http://localhost/webservices/rest.php"
    api_version: str = "1.3"
    timeout: float = 30.0
    user: str | None = None
    pwd: str | None = None
    token: str | None = None

    @property
    def has_auth(self) -> bool:
        return bool(self.user and self.pwd) or bool(self.token)


class LlmConfig(RuntimeSectionConfig):
    """LLM endpoint settings — runtime-editable section "llm"."""

    SECRET_FIELDS: ClassVar[frozenset[str]] = frozenset({"api_key"})

    base_url: str = "http://localhost:1234/v1"
    model: str | None = None
    api_key: str | None = None
    # Tag names treated as inline reasoning blocks in LLM output and stripped
    # before parsing/posting (as <tag>…</tag> pairs or orphan halves).
    # Asymmetric markers (e.g. Gemma's <context|>…<|context>) are not supported.
    think_tags: list[str] = ["think", "thinking", "reasoning"]


class SecurityConfig(RuntimeSectionConfig):
    """Shared secrets for the public endpoints — runtime-editable section "security".

    None disables auth for the corresponding endpoint group; the first-run
    setup wizard is expected to set both before exposing the service.
    """

    SECRET_FIELDS: ClassVar[frozenset[str]] = frozenset({"webhook_token", "admin_token"})

    webhook_token: str | None = None
    admin_token: str | None = None


def missing_setup(itop: ItopConfig, llm: LlmConfig) -> list[str]:
    """Setup steps still required before the assistant may process tickets."""
    missing = []
    if not itop.has_auth:
        missing.append("iTop credentials (itop: user+pwd or token)")
    if not llm.model:
        missing.append("LLM model (llm: model)")
    return missing


class EnrichmentConfig(BaseModel):
    enabled: bool = True
    classes: list[str] = ["UserRequest", "Incident"]
    classification_enabled: bool = True
    max_rounds: int = 2
    max_classify_rounds: int = 2
    # Per-node model overrides; None falls back to the global llm_model
    classify_model: str | None = None
    evaluate_model: str | None = None
    enrich_model: str | None = None
    classify_fallback_note: str = "Could not determine the request category. Manual classification required."
    classify_service_oql: str = _CLASSIFY_SERVICE_OQL
    classify_subcategory_oql: str = _CLASSIFY_SUBCATEGORY_OQL


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        yaml_file=str(_ROOT / "config.yaml"),
        env_file=str(_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # App
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    log_level: str = "INFO"
    webhook_token: SecretStr | None = None
    # Shared secret for the admin API (X-Admin-Token header); None = no auth
    admin_token: SecretStr | None = None
    # Directory with per-deployment prompt overrides (see prompt_store.FilePromptStore)
    prompts_dir: Path | None = None

    # iTop
    itop_url: str = "http://localhost/webservices/rest.php"
    itop_api_version: str = "1.3"
    itop_timeout: float = 30.0
    itop_user: str | None = None
    itop_pwd: SecretStr | None = None
    itop_token: SecretStr | None = None

    # LLM
    llm_base_url: str = "http://localhost:1234/v1"
    llm_model: str | None = None
    llm_api_key: SecretStr | None = None
    llm_think_tags: list[str] = ["think", "thinking", "reasoning"]

    # Redis
    redis_url: str = "redis://localhost:6379"
    state_ttl_days: int = 30
    # How long processing-run journal entries are kept
    run_ttl_days: int = 7

    # iTop datamodel mapping
    ticket_mapping: TicketMappingConfig = TicketMappingConfig()

    # Business modules
    enrichment: EnrichmentConfig = EnrichmentConfig()

    # Env/yaml values act as *defaults* for the runtime-editable sections
    # below: RedisConfigStore resolves a section via getattr(settings, name),
    # so overrides stored through the setup API take priority over these.

    @property
    def itop(self) -> ItopConfig:
        return ItopConfig(
            url=self.itop_url,
            api_version=self.itop_api_version,
            timeout=self.itop_timeout,
            user=self.itop_user,
            pwd=self.itop_pwd.get_secret_value() if self.itop_pwd else None,
            token=self.itop_token.get_secret_value() if self.itop_token else None,
        )

    @property
    def llm(self) -> LlmConfig:
        return LlmConfig(
            base_url=self.llm_base_url,
            model=self.llm_model,
            api_key=self.llm_api_key.get_secret_value() if self.llm_api_key else None,
            think_tags=self.llm_think_tags,
        )

    @property
    def security(self) -> SecurityConfig:
        return SecurityConfig(
            webhook_token=self.webhook_token.get_secret_value() if self.webhook_token else None,
            admin_token=self.admin_token.get_secret_value() if self.admin_token else None,
        )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return env_settings, dotenv_settings, YamlConfigSettingsSource(settings_cls)


@lru_cache
def get_settings() -> Settings:
    return Settings()
