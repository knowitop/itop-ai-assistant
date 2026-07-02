from functools import lru_cache
from pathlib import Path

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
    llm_model: str
    llm_api_key: SecretStr

    # Redis
    redis_url: str = "redis://localhost:6379"
    state_ttl_days: int = 30

    # iTop datamodel mapping
    ticket_mapping: TicketMappingConfig = TicketMappingConfig()

    # Business modules
    enrichment: EnrichmentConfig = EnrichmentConfig()

    @model_validator(mode="after")
    def check_itop_auth(self) -> "Settings":
        has_basic = self.itop_user and self.itop_pwd
        has_token = bool(self.itop_token)
        if not has_basic and not has_token:
            raise ValueError("iTop auth required: set itop_user+itop_pwd or itop_token")
        return self

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
