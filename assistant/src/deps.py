from dataclasses import dataclass
from pathlib import Path

import redis.asyncio as aioredis
from langchain_openai import ChatOpenAI

from config import Settings
from config_store import ConfigStore, StaticConfigStore
from itop_client import Itop
from prompt_store import FilePromptStore, PromptStore
from state.ticket_state import TicketStateManager
from ticket_repository import TicketRepository

_DEFAULT_PROMPTS_DIR = Path(__file__).parent.parent / "prompts"  # assistant/prompts


@dataclass
class AppDeps:
    """Application-wide dependencies, assembled once at startup (composition root)."""

    settings: Settings
    itop_client: Itop
    ticket_repo: TicketRepository
    state_manager: TicketStateManager
    config_store: ConfigStore
    prompt_store: PromptStore

    async def aclose(self) -> None:
        await self.itop_client.aclose()
        await self.state_manager.aclose()


def create_llm(settings: Settings, model: str | None = None) -> ChatOpenAI:
    """Create an LLM client. `model` overrides the default `settings.llm_model`."""
    return ChatOpenAI(
        model=model or settings.llm_model,
        api_key=settings.llm_api_key,
        base_url=settings.llm_base_url,
    )


def build_deps(settings: Settings) -> AppDeps:
    itop_client = Itop(
        url=settings.itop_url,
        version=settings.itop_api_version,
        auth_user=settings.itop_user,
        auth_pwd=settings.itop_pwd.get_secret_value() if settings.itop_pwd else None,
        auth_token=settings.itop_token.get_secret_value() if settings.itop_token else None,
        timeout=settings.itop_timeout,
    )
    redis = aioredis.from_url(settings.redis_url, decode_responses=True)
    state_manager = TicketStateManager(redis, ttl_seconds=settings.state_ttl_days * 24 * 60 * 60)
    return AppDeps(
        settings=settings,
        itop_client=itop_client,
        ticket_repo=TicketRepository(itop_client, settings.ticket_mapping),
        state_manager=state_manager,
        config_store=StaticConfigStore(settings),
        prompt_store=FilePromptStore(_DEFAULT_PROMPTS_DIR, settings.prompts_dir),
    )
