from functools import lru_cache
from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)

from itop_ai_assistant.llm_providers import DEFAULT_PROVIDER, PROVIDERS, get_provider

_PACKAGE_DIR = Path(__file__).parent  # itop_ai_assistant/ — ships config.yaml


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

    # Which kind of endpoint this is — see llm_providers.PROVIDERS. The default
    # is the historical behaviour: any OpenAI-compatible URL.
    provider: str = DEFAULT_PROVIDER
    base_url: str | None = None
    model: str | None = None
    api_key: str | None = None
    # Tag names treated as inline reasoning blocks in LLM output and stripped
    # before parsing/posting (as <tag>…</tag> pairs or orphan halves).
    # Asymmetric markers (e.g. Gemma's <context|>…<|context>) are not supported.
    think_tags: list[str] = ["think", "thinking", "reasoning"]
    # Passed verbatim to the provider's client: temperature, max_tokens,
    # timeout, max_retries, reasoning_effort — whatever it accepts.
    params: dict[str, Any] = {}
    # Does *this* server accept tool_choice="any"? None = take the provider's
    # answer; only asked where the provider has none (openai_compatible fronts
    # both vLLM, which accepts, and DeepSeek, which returns HTTP 400).
    supports_forced_tool_choice: bool | None = None

    # Reserved by create_llm — allowing them in `params` would silently
    # override the section's own fields.
    _RESERVED_PARAMS: ClassVar[frozenset[str]] = frozenset({"model", "model_provider", "base_url", "api_key"})

    @model_validator(mode="after")
    def check_provider_and_params(self) -> "LlmConfig":
        get_provider(self.provider)  # raises ValueError listing the known ones
        clashing = self._RESERVED_PARAMS & set(self.params)
        if clashing:
            raise ValueError(
                f"llm.params may not contain {sorted(clashing)} — those come from the section's own fields"
            )
        return self

    @property
    def endpoint_forces_tool_choice(self) -> bool:
        """Does the endpoint accept tool_choice="any"? Explicit answer wins over the registry.

        Whether to *use* it is up to the agent — see llm_providers.
        """
        if self.supports_forced_tool_choice is not None:
            return self.supports_forced_tool_choice
        return bool(PROVIDERS[self.provider].supports_forced_tool_choice)


class EmbeddingsConfig(RuntimeSectionConfig):
    """Embedding endpoint settings — runtime-editable section "embeddings".

    Optional: the vector store stays off without it. The model must be
    multilingual (tickets are ru/en mixed) and `dimension` must match what
    the model actually returns — verified by POST /api/setup/test-embeddings.
    """

    SECRET_FIELDS: ClassVar[frozenset[str]] = frozenset({"api_key"})

    # The full OpenAI-compatible prefix the provider documents for embeddings —
    # not just a bare host:port. Providers whose base_url is bare host:port for
    # *chat* (llm_providers.ollama) still need the full prefix here: Ollama is
    # "/v1", Google's Gemini API compat layer is "/v1beta/openai" — a bare host
    # 404s with a plain-text body (see TASK-007).
    base_url: str | None = None
    model: str | None = None
    api_key: str | None = None
    # Qdrant has no hard ceiling here; the bound is a sanity check, and ADR-006
    # points the default the other way — MRL truncation to 512 or 256.
    dimension: int = Field(default=1024, gt=0, le=4096)
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
    """Setup steps still required before the assistant may process tickets.

    Which LLM fields count as required depends on the provider: a local
    OpenAI-compatible server needs a URL and no key, Gemini the opposite.
    """
    missing = []
    if not itop.url:
        missing.append("iTop REST API URL (itop: url)")
    if not itop.has_auth:
        missing.append("iTop credentials (itop: user+pwd or token)")
    provider = PROVIDERS[llm.provider]  # validated by LlmConfig
    if provider.base_url_mode == "required" and not llm.base_url:
        missing.append("LLM endpoint (llm: base_url)")
    if provider.api_key_mode == "required" and not llm.api_key:
        missing.append("LLM API key (llm: api_key)")
    if not llm.model:
        missing.append("LLM model (llm: model)")
    return missing


class IntakeConfig(BaseModel):
    """The ticket-processing module: classify, ask, hand off.

    One tool-calling agent per ticket rather than a fixed sequence of steps —
    the model decides which tool to call next, the tools enforce the
    invariants.
    """

    enabled: bool = True
    classes: list[str] = ["UserRequest", "Incident"]
    max_rounds: int = 2
    max_classify_rounds: int = 2
    # Budget of model calls per run; without it a looping agent burns tokens
    # until the ticket is abandoned. Catalog + subcategories + classify +
    # similar tickets + question/handoff + slack.
    max_iterations: int = 9
    # One override for the whole module (the agent has a single loop); None
    # falls back to the global llm_model. It must be a reliable tool-caller —
    # a model that answers in prose instead of calling a tool burns the run.
    model: str | None = None
    classify_fallback_note: str = "Could not determine the request category. Manual classification required."
    handoff_fallback_note: str = "AI intake finished without a summary. Manual review required."
    # Similar solved tickets quoted in the handoff note (only when the vector
    # store and the embeddings endpoint are configured). The window is a range
    # over the modification date, never a substitute for the status filter —
    # a reopened ticket keeps its old resolution date (ADR-005, rule 2).
    similar_max_age_days: int = Field(default=365, gt=0)
    # Business parameter of the "similar solved" scenario — not tied to
    # `VectorClassConfig.index_values` (the matching default is a coincidence
    # for tickets, not a shared source of truth, see ADR-017).
    resolved_statuses: list[str] = ["resolved", "closed"]
    # Asked of the index; more than `similar_top` because candidates the
    # requester's iTop no longer returns are dropped afterwards (ADR-003)
    similar_candidates: int = Field(default=15, gt=0)
    similar_top: int = Field(default=5, gt=0)
    # Absolute floor on the Qdrant cosine score (range [-1, 1]) below which a
    # candidate is dropped regardless of rank — top-N alone does not
    # guarantee relevance, only relative rank among whatever `candidates`
    # happened to return (TASK-011). 0.5 is an engineering guess, not
    # calibrated against this deployment's embeddings model; tune it after a
    # live check against real similar/unrelated pairs.
    similar_min_score: float = Field(default=0.5, ge=-1.0, le=1.0)
    classify_service_oql: str = _CLASSIFY_SERVICE_OQL
    classify_subcategory_oql: str = _CLASSIFY_SUBCATEGORY_OQL


class SelfCheckConfig(BaseModel):
    """The smoke module: it touches every seam and changes nothing.

    Its job is to prove the platform's own contracts on a live deployment —
    a config section, a prompt file, an LLM call, an iTop read and a journal
    entry, reached through the same trigger registry every business module
    uses. It writes nothing anywhere, which is why it is safe to schedule.
    """

    # Read at startup like intake's: off by default, because nobody wants a
    # fresh deployment calling a model on a timer for no business reason
    enabled: bool = False
    interval_seconds: int = Field(default=900, gt=0)
    # Cheapest read that proves the connection and the credentials at once
    probe_oql: str = "SELECT Service"
    model: str | None = None


class VectorClassConfig(BaseModel):
    """Per-class vector index settings (one entry per indexed object class).

    Every indexed class must expose a last-modification datetime and a
    "relevance" attribute — the VectorSource contract (`vector/source.py`).
    Which attributes those are is the source's concern (tickets map them via
    `ticket_mapping`); this config holds only the relevance *values*.
    """

    # Values of the class's relevance attribute that keep an object in the
    # index (similar-tickets searches want resolved knowledge, not open
    # noise); [] = index every object of the class
    index_values: list[str] = []
    # Chunking profile: which semantic fields feed which chunk kinds
    profile: dict[str, list[str]] = {}


_TICKET_PROFILE = {
    "profile": ["title", "service", "subcategory"],
    "body": ["description"],
    "solution": ["solution"],
}


class VectorConfig(BaseModel):
    """Vector index settings — infrastructure section "vector" (setup API).

    Off by default: the base deployment stays Redis-only. The chunking
    profiles and sweep settings are consumed by the indexer (Stage 2);
    they live here from the start so the section schema is stable.
    """

    enabled: bool = False
    # Indexed object classes with their per-class settings
    classes: dict[str, VectorClassConfig] = {
        "UserRequest": VectorClassConfig(index_values=["resolved", "closed"], profile=_TICKET_PROFILE),
        "Incident": VectorClassConfig(index_values=["resolved", "closed"], profile=_TICKET_PROFILE),
    }
    sweep_interval_seconds: int = Field(default=300, gt=0)
    sweep_page_size: int = Field(default=100, gt=0)
    # Pause between iTop pages so a backfill doesn't hammer the REST API
    sweep_throttle_seconds: float = Field(default=0.5, ge=0)
    reconcile_interval_days: int = Field(default=7, gt=0)
    max_chunk_tokens: int = Field(default=480, gt=0)
    log_entries_per_chunk: int = Field(default=5, gt=0)


class Settings(BaseSettings):
    # config.yaml ships inside the package, so it is found from any working
    # directory. `.env` cannot be: it is gitignored, per-developer and absent
    # from the image (compose passes env vars directly) — it is read relative
    # to the working directory, which is `assistant/` locally and `/app` in
    # the container.
    model_config = SettingsConfigDict(
        yaml_file=str(_PACKAGE_DIR / "config.yaml"),
        env_file=".env",
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
    # Where the built admin SPA lives. The image sets it explicitly; unset =
    # probe the source checkout (see main._find_ui_dist)
    ui_dist_dir: Path | None = None

    # iTop
    itop_url: str | None = None
    itop_api_version: str = "1.3"
    itop_timeout: float = 30.0
    itop_user: str | None = None
    itop_pwd: SecretStr | None = None
    itop_token: SecretStr | None = None

    # LLM
    llm_provider: str = DEFAULT_PROVIDER
    llm_base_url: str | None = None
    llm_model: str | None = None
    llm_api_key: SecretStr | None = None
    llm_think_tags: list[str] = ["think", "thinking", "reasoning"]
    # JSON in env (LLM_PARAMS='{"temperature": 0.2}'); config.yaml is nicer
    llm_params: dict[str, Any] | None = {}
    llm_supports_forced_tool_choice: bool | None = None

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

    # Qdrant (vector store) — bootstrap, env-only like redis_url.
    # None = vector features unavailable; the app runs Redis-only.
    qdrant_url: str | None = None

    # iTop datamodel mapping
    ticket_mapping: TicketMappingConfig = TicketMappingConfig()

    # Business modules
    intake: IntakeConfig = IntakeConfig()
    selfcheck: SelfCheckConfig = SelfCheckConfig()

    # Vector store (infrastructure; editable via /api/setup/vector)
    vector: VectorConfig = VectorConfig()

    @field_validator("llm_params", "llm_supports_forced_tool_choice", mode="before")
    @classmethod
    def blank_env_means_unset(cls, value: Any) -> Any:
        """A blank line in .env (LLM_PARAMS=) means "not set", not a parse error.

        Without this the app would refuse to boot on a freshly copied
        .env.dist, and no field here is supposed to be required at startup.
        """
        return None if value == "" else value

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
            provider=self.llm_provider,
            base_url=self.llm_base_url,
            model=self.llm_model,
            api_key=self.llm_api_key.get_secret_value() if self.llm_api_key else None,
            think_tags=self.llm_think_tags,
            params=self.llm_params or {},
            supports_forced_tool_choice=self.llm_supports_forced_tool_choice,
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
