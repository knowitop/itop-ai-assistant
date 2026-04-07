from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr, model_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)

_ROOT = Path(__file__).parent.parent  # assistant/


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

    # iTop
    itop_url: str = "http://localhost/webservices/rest.php"
    itop_user: str | None = None
    itop_pwd: SecretStr | None = None
    itop_token: SecretStr | None = None

    # LLM
    llm_base_url: str = "http://localhost:1234/v1"
    llm_model: str
    llm_api_key: SecretStr

    # Redis
    redis_url: str = "redis://localhost:6379"

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
