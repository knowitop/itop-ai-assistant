import logging
from functools import lru_cache
from pathlib import Path
from typing import Any, ClassVar, TypeVar

from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)

from itop_ai_assistant.core.llm_providers import DEFAULT_PROVIDER, PROVIDERS, get_provider
from itop_ai_assistant.domain.families import SCHEMAS
from itop_ai_assistant.domain.faq_schema import FAQ_SCHEMA
from itop_ai_assistant.domain.schema import FieldKind, FieldSpec, Role, Schema
from itop_ai_assistant.domain.tickets_schema import TICKET_SCHEMA

logger = logging.getLogger(__name__)

_PACKAGE_DIR = Path(__file__).parent  # itop_ai_assistant/ — ships config.yaml

TConfig = TypeVar("TConfig", bound=BaseModel)


class DeclaredField(BaseModel):
    """A semantic field an administrator added to a family.

    The same thing a `FieldSpec` in code is (ADR-034), minus the attribute
    code: that lives in `fields` beside every other field's, so there is one
    table of attribute codes and not two. What such a field is *for* is its
    roles — an organization that grants access, a piece of what the object is
    about — and a field with none is carried into the index and nothing else.

    No `datetime` and no `log`: both are read by a mechanism that has to be
    wired to them (the sweep cursor, a chunk fragment), and a second one of
    either is not something a declaration can hook up.
    """

    kind: FieldKind
    multi: bool = False
    roles: list[Role] = []
    description: str = ""

    @field_validator("kind")
    @classmethod
    def readable_by_a_declaration(cls, kind: FieldKind) -> FieldKind:
        if kind in (FieldKind.DATETIME, FieldKind.LOG):
            raise ValueError(f"a declared field cannot be a {kind.value!r} — only text, id or enum")
        return kind


class MappingConfig(BaseModel):
    """How one family's semantics map onto the customer's iTop datamodel.

    Only what this deployment changed. The baseline is the family's own
    declaration (`domain/schema.py::Schema.sources`), so a field added to a
    schema needs no edit here, and a saved mapping does not go stale by being
    a copy of the code it was copied from.

    `class_overrides` are point differences between classes sharing one
    mapping — a class without some attribute (`None`) or with a renamed one,
    merged over `fields` for that class. Only families with several classes
    ever need them.
    """

    fields: dict[str, str | None] = {}
    class_overrides: dict[str, dict[str, str | None]] = {}
    #: Fields this deployment added to the family, by name. Their attribute
    #: codes are in `fields` like everyone else's.
    declared: dict[str, DeclaredField] = {}

    def declared_specs(self) -> tuple[FieldSpec, ...]:
        """What this deployment added, as the same declaration the code
        writes. `source` is None because a declared field has no default —
        where its value comes from is entirely `fields`."""
        return tuple(
            FieldSpec(
                name=name,
                kind=field.kind,
                source=None,
                multi=field.multi,
                roles=frozenset(field.roles),
                description=field.description,
                from_config=True,
            )
            for name, field in self.declared.items()
        )


class MappingsConfig(BaseModel):
    """The datamodel mapping of every family — runtime-editable section
    "mappings".

    One section rather than one per family: a family is a declaration now
    (ADR-034), and a section per family would be the one place a new family
    still cost a pydantic class, a `SETUP_SECTIONS` entry and a UI form.
    """

    families: dict[str, MappingConfig] = {
        # Stock iTop's Incident has no request_type. A default rather than a
        # fact of the schema: a deployment whose Incident does carry one says
        # so by overriding this entry, and nothing in the code has to change.
        TICKET_SCHEMA.name: MappingConfig(class_overrides={"Incident": {"request_type": None}}),
        FAQ_SCHEMA.name: MappingConfig(),
    }

    def for_family(self, family: str) -> MappingConfig:
        """What this deployment says about one family — an empty mapping when
        it says nothing, which means "the declaration as written"."""
        return self.families.get(family, MappingConfig())

    def schemas(self) -> dict[str, Schema]:
        """Every family as this deployment has it: what the code declares plus
        what the administrator added.

        The one place the two are merged. Everything that reads a field —
        the repository, the vector source, the admin forms — asks here, so a
        declared field is a field in exactly the sense a built-in one is.
        """
        return {name: schema.extended(self.for_family(name).declared_specs()) for name, schema in SCHEMAS.items()}

    @model_validator(mode="after")
    def check_field_names(self) -> "MappingsConfig":
        """Cross-check a section cannot make on its own: whether a name in it
        is a field of the family it configures.

        A family nothing declares is kept and warned about rather than
        refused: it configures nothing, and rejecting it would take the whole
        section down with it on start ([[ADR-026]]).
        """
        for family, cfg in self.families.items():
            schema = SCHEMAS.get(family)
            if schema is None:
                logger.warning(f"mappings: family {family!r} is not declared anywhere — the section does nothing")
                continue
            taken = sorted(name for name in cfg.declared if schema.spec(name) is not None)
            if taken:
                raise ValueError(
                    f"mappings.{family}.declared: {taken} — the {family!r} family already has fields by those names"
                )
            extended = schema.extended(cfg.declared_specs())
            extended.resolve(cfg.fields, by=f"mappings.{family}.fields")
            for obj_class, overrides in cfg.class_overrides.items():
                extended.resolve(overrides, by=f"mappings.{family}.class_overrides[{obj_class!r}]")
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
    # override the section's own fields. `callbacks` is on the list for a
    # sharper reason than the others: it carries the telemetry counter
    # (`core/llm_counters.py`), and a value here would replace it rather than
    # add to it — the counting would stop, and nothing would say so.
    _RESERVED_PARAMS: ClassVar[frozenset[str]] = frozenset(
        {"model", "model_provider", "base_url", "api_key", "callbacks"}
    )

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
    # No upper bound: this declares the model's native vector length rather than
    # choosing one — the OpenAI `dimensions` truncation parameter is deliberately
    # not sent (see vector.adapters.embedder), so changing the length means
    # changing the model. Qdrant has no ceiling of its own either, and the real
    # check is elsewhere: POST /api/setup/test-embeddings measures what the
    # endpoint returns, and EmbeddingsClient re-checks every batch against this
    # value. Any number here would only reject a legitimate config.
    dimension: int = Field(default=1024, gt=0)
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


class PlatformConfig(BaseModel):
    """Switches that describe the installation, not a module — section "platform".

    Not a `RuntimeSectionConfig`: nothing here is a secret. Kept out of the
    `itop` section on purpose — `ItopConnection` rebuilds its client whenever
    that section's fingerprint changes, and toggling a mode would close a
    connection pool shared with every principal view over nothing the
    connection cares about.
    """

    dry_run: bool = Field(
        default=False,
        title="Dry run",
        description=(
            "Runs proceed exactly as they would in production — the catalogue is read, the model is called, "
            "the run journal records every step — but nothing is written to iTop: no field change, no public "
            "or private log entry. Applies from the next run, no restart."
        ),
    )


class TelemetryConfig(BaseModel):
    """Whether the anonymous daily document leaves this installation — section "telemetry".

    One field, and nothing else can be configured here: the receiver's address,
    the application id and the ingest key are our own constants and travel in
    the image. The ingest key is not a secret by nature — analytics vendors
    ship it inside client applications — so it needs neither a field here nor
    masking at the setup API boundary (REQ-009 R5). The side benefit is that a
    section with nothing to configure cannot be configured wrongly: there is
    no field to point at somebody else's receiver.

    Runtime-editable rather than env-only, unlike tracing (ADR-029, where the
    switch belongs to the deployment): sending is a periodic task, not a
    global instrumentor installed once per process, and this is a switch that
    limits *us* — such a thing must not be less reachable than what it limits.
    `TELEMETRY_ENABLED` stays as the deployment-time default and neither
    blocks the button nor outranks it (`.claude/rules/config.md`).
    """

    # On by default, which R5 asks for and which only became allowable once
    # the sending was visible: the System screen carries the switch and the
    # installation id, `GET /api/telemetry/preview` shows the exact document
    # that would leave today, the setup wizard says so on its welcome screen
    # before a single setting is saved, and `docs/telemetry.md` describes the
    # whole of it. An installation that sends data out and cannot show which
    # gets a product blacklisted whole — that is what this default waited for.
    enabled: bool = Field(
        default=True,
        title="Send anonymous usage telemetry",
        description=(
            "One aggregate document a day: counts of what this installation did, which modules are on, "
            "and the versions it runs. Never ticket content, names, addresses or keys. Applies without "
            "a restart."
        ),
    )


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
    # Default for section "platform" — the installation-wide dry run (REQ-006)
    dry_run: bool = False
    # Default for section "telemetry" (REQ-009 R5). A deployment-time value —
    # an unattended install, an image built for one customer — never a lock:
    # the runtime override wins, as it does for every other section. Says
    # nothing about a build we did not publish: those send nothing whatever
    # this holds (`util/build_info.py::is_release_build`).
    telemetry_enabled: bool = True
    # Marks every signal as a test one, so the receiver keeps it out of
    # production queries. Ours, not the administrator's: it exists for the
    # stand we verify releases on (ADR-031 asks for that check before every
    # release touching telemetry), and a stand that cannot say "this is a
    # test" inflates the installation count by one forever.
    #
    # It marks and nothing else. Whether a build may send at all is the flag
    # below — the two used to be one, and a stand needing both is a smaller
    # cost than "mark this as test" and "send from an unpublished build"
    # being impossible to ask for separately.
    telemetry_test_mode: bool = False
    # Lets a build we did not publish send anyway (`telemetry/sender.py`).
    # Two callers: the verification stand, which sets it together with the
    # flag above, and an installation deployed from source onto a real server
    # — which is otherwise never counted, because nothing can tell it from a
    # developer's laptop. Setting this says "count me", and it is the
    # administrator's to set, unlike the test mark.
    telemetry_allow_unpublished_build: bool = False
    # An admin SPA to serve instead of the one inside the package — for a
    # deployment that builds its own (see main._find_ui_dist)
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

    # LLM tracing (ADR-029) — bootstrap, env-only like qdrant_url, and for a
    # sharper reason: the instrumentation is installed once per process, so
    # there is nothing a runtime section could switch. Off by default; when
    # off, no tracing package is imported and no span leaves the process.
    tracing_enabled: bool = False
    # OTLP/HTTP: the full path to /v1/traces, not a bare host:port. The
    # exporter speaks one protocol (`core/tracing_otel.py`), so a gRPC port
    # (:4317) here would silently export nothing.
    tracing_endpoint: str = "http://localhost:6006/v1/traces"
    tracing_project_name: str = "itop-ai-assistant"

    # iTop datamodel mapping, one entry per object family
    mappings: MappingsConfig = MappingsConfig()

    # Business modules — config.py does not know their field names, only
    # this raw bucket. A module resolves its own section via
    # `module_defaults(name, its_own_model)`, both at registration
    # (`agents/<module>/pipeline.py::register`) and through
    # `RedisConfigStore` (which merges Redis overrides on top). The same
    # bucket also serves `vector` (`vector/config.py`, TASK-036): it is not a
    # business module, but has no `Settings` attribute either, so it falls
    # back to the exact same path — `module_defaults` needs nothing but a
    # name and a model, not a module registration.
    module_config: dict[str, dict[str, Any]] = {}

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
    # Business-module sections have no such attribute — see `module_defaults`.

    def module_defaults(self, name: str, model: type[TConfig]) -> TConfig:
        """Env/yaml defaults for a business module's own config section.

        Unlike `itop`/`llm`/... below, `Settings` does not know this
        section's fields — `module_config[name]` is a raw dict, validated
        against the model the module itself registers
        (`ModuleInfo.config_model`). Called both by a module's own
        `register()` (to read `enabled`/`classes` at startup, before the
        registry exists) and by `RedisConfigStore` as the fallback when a
        section is not a `Settings` attribute.
        """
        return model(**self.module_config.get(name, {}))

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

    @property
    def platform(self) -> PlatformConfig:
        return PlatformConfig(dry_run=self.dry_run)

    @property
    def telemetry(self) -> TelemetryConfig:
        return TelemetryConfig(enabled=self.telemetry_enabled)

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
