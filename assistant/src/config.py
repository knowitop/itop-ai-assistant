from functools import lru_cache
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, Field, SecretStr, model_validator
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
    solution: str | None = "solution"
    last_update: str | None = "last_update"
    # Stock iTop attribute for ticket creation time; custom datamodels remap
    # via class_overrides (Incident needs none — it has start_date too)
    created_at: str | None = "start_date"


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

    url: str | None = None
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

    base_url: str | None = None
    model: str | None = None
    api_key: str | None = None
    # Tag names treated as inline reasoning blocks in LLM output and stripped
    # before parsing/posting (as <tag>…</tag> pairs or orphan halves).
    # Asymmetric markers (e.g. Gemma's <context|>…<|context>) are not supported.
    think_tags: list[str] = ["think", "thinking", "reasoning"]


class EmbeddingsConfig(RuntimeSectionConfig):
    """Embedding endpoint settings — runtime-editable section "embeddings".

    Optional: the vector store stays off without it. The model must be
    multilingual (tickets are ru/en mixed) and `dimension` must match what
    the model actually returns — verified by POST /api/setup/test-embeddings.
    """

    SECRET_FIELDS: ClassVar[frozenset[str]] = frozenset({"api_key"})

    base_url: str | None = None  # OpenAI-compatible, includes /v1 (like llm.base_url)
    model: str | None = None
    api_key: str | None = None
    # pgvector HNSW indexes support halfvec up to 4000 dims
    dimension: int = Field(default=1024, gt=0, le=4000)
    batch_size: int = Field(default=32, gt=0)
    timeout: float = 30.0


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
    if not itop.url:
        missing.append("iTop REST API URL (itop: url)")
    if not itop.has_auth:
        missing.append("iTop credentials (itop: user+pwd or token)")
    if not llm.base_url:
        missing.append("LLM endpoint (llm: base_url)")
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


class VectorConfig(BaseModel):
    """Vector index settings — infrastructure section "vector" (setup API).

    Off by default: the base deployment stays Redis-only. The chunking
    profiles and sweep settings are consumed by the indexer (Stage 2);
    they live here from the start so the section schema is stable.
    """

    enabled: bool = False
    # Isolates deployments sharing one Postgres (staging/prod)
    env: str = "main"
    classes: list[str] = ["UserRequest", "Incident"]
    # Per-class chunking profiles: which semantic fields feed which chunk kinds
    profiles: dict[str, dict[str, list[str]]] = {
        "UserRequest": {
            "profile": ["title", "service", "subcategory"],
            "body": ["description"],
            "solution": ["solution"],
        },
        "Incident": {"profile": ["title", "service", "subcategory"], "body": ["description"], "solution": ["solution"]},
    }
    sweep_interval_seconds: int = Field(default=300, gt=0)
    sweep_page_size: int = Field(default=100, gt=0)
    # Pause between iTop pages so a backfill doesn't hammer the REST API
    sweep_throttle_seconds: float = Field(default=0.5, ge=0)
    reconcile_interval_days: int = Field(default=7, gt=0)
    max_chunk_tokens: int = Field(default=480, gt=0)
    log_entries_per_chunk: int = Field(default=5, gt=0)
    # Only objects in these statuses are indexed (similar-tickets searches
    # want resolved knowledge, not open noise)
    index_statuses: list[str] = ["resolved", "closed"]


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
    # Bearer token for the admin API (Authorization header); None = no auth
    admin_token: SecretStr | None = None
    # Directory with per-deployment prompt overrides (see prompt_store.FilePromptStore)
    prompts_dir: Path | None = None

    # iTop
    itop_url: str | None = None
    itop_api_version: str = "1.3"
    itop_timeout: float = 30.0
    itop_user: str | None = None
    itop_pwd: SecretStr | None = None
    itop_token: SecretStr | None = None

    # LLM
    llm_base_url: str | None = None
    llm_model: str | None = None
    llm_api_key: SecretStr | None = None
    llm_think_tags: list[str] = ["think", "thinking", "reasoning"]

    # Embeddings (vector store)
    embeddings_base_url: str | None = None
    embeddings_model: str | None = None
    embeddings_api_key: SecretStr | None = None
    embeddings_dimension: int = 1024
    embeddings_batch_size: int = 32
    embeddings_timeout: float = 30.0

    # Redis
    redis_url: str = "redis://localhost:6379"
    state_ttl_days: int = 30
    # How long processing-run journal entries are kept
    run_ttl_days: int = 7

    # Postgres (vector store) — bootstrap, env-only like redis_url.
    # None = vector features unavailable; the app runs Redis-only.
    # Format: postgresql+asyncpg://user:pass@host:5432/dbname
    database_url: str | None = None

    # iTop datamodel mapping
    ticket_mapping: TicketMappingConfig = TicketMappingConfig()

    # Business modules
    enrichment: EnrichmentConfig = EnrichmentConfig()

    # Vector store (infrastructure; editable via /api/setup/vector)
    vector: VectorConfig = VectorConfig()

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
    def embeddings(self) -> EmbeddingsConfig:
        return EmbeddingsConfig(
            base_url=self.embeddings_base_url,
            model=self.embeddings_model,
            api_key=self.embeddings_api_key.get_secret_value() if self.embeddings_api_key else None,
            dimension=self.embeddings_dimension,
            batch_size=self.embeddings_batch_size,
            timeout=self.embeddings_timeout,
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
