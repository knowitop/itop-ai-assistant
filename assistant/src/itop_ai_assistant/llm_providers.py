"""What the service knows about the LLM endpoints it can talk to.

One entry per supported provider: which `init_chat_model` provider string to
use, which connection fields make sense (a base URL is required for a local
OpenAI-compatible server, meaningless for Gemini), and whether the endpoint
accepts a forced `tool_choice`.

The registry answers "what can this endpoint do", never "what should the
assistant do with it" — that belongs to the calling module. The intake agent
forces `tool_choice` wherever it is accepted because plain text from its model
reaches nobody; a future agent that needs prose simply will not ask.

**Forced `tool_choice` per provider** (write it as `"any"`, never
`"required"` — LangChain normalizes per provider, `langchain_openai` maps
`"any"` → `"required"` because OpenAI has no `"any"`):

- OpenAI, and vLLM behind an OpenAI-compatible URL — `tool_choice: "required"`
  is a standard Chat Completions parameter;
- Google — `ChatGoogleGenerativeAI.bind_tools` documents `"any"` and
  `"required"` as equivalent (native `function_calling_config` mode ANY);
- **not** DeepSeek V4 (`deepseek-v4-pro`, `deepseek-v4-flash`): both are always
  in thinking mode, which rejects `"required"` and named-function choices with
  HTTP 400 "Thinking mode does not support this tool_choice"
  (deepseek-ai/DeepSeek-V3#1376, open). DeepSeek's own guidance is to omit the
  parameter and accept that the model sometimes answers in prose instead;
- **not** Ollama, whose OpenAI-compatible layer drops `tool_choice`.

DeepSeek and vLLM are both reached through `openai_compatible`, so the registry
cannot answer for that entry — hence `None`, "ask the deployment owner"
(`llm.supports_forced_tool_choice`). Nothing else is allowed to be unknown:
a `None` here is a question the setup UI must put to the user.
"""

from dataclasses import dataclass
from typing import Literal

FieldMode = Literal["required", "optional", "unused"]


@dataclass(frozen=True)
class LlmProvider:
    """One supported way to reach a chat model."""

    id: str
    label: str
    # Provider string for langchain's init_chat_model
    langchain_provider: str
    base_url_mode: FieldMode
    base_url_placeholder: str | None
    api_key_mode: FieldMode
    # Does the endpoint accept tool_choice="any"? None = depends on the server
    # behind it, ask the user. Whether to use it is the agent's call.
    supports_forced_tool_choice: bool | None
    notes: str = ""


PROVIDERS: dict[str, LlmProvider] = {
    provider.id: provider
    for provider in (
        LlmProvider(
            id="openai_compatible",
            label="OpenAI-compatible endpoint",
            langchain_provider="openai",
            base_url_mode="required",
            base_url_placeholder="http://localhost:1234/v1",
            # Local servers ignore the key entirely; cloud gateways require one
            api_key_mode="optional",
            supports_forced_tool_choice=None,
            notes="LM Studio, Ollama's OpenAI layer, vLLM, LiteLLM Proxy, DeepSeek, Azure OpenAI, OpenRouter",
        ),
        LlmProvider(
            id="openai",
            label="OpenAI",
            langchain_provider="openai",
            base_url_mode="unused",
            base_url_placeholder=None,
            api_key_mode="required",
            supports_forced_tool_choice=True,
        ),
        LlmProvider(
            id="google_genai",
            label="Google Gemini",
            langchain_provider="google_genai",
            base_url_mode="unused",
            base_url_placeholder=None,
            api_key_mode="required",
            supports_forced_tool_choice=True,
        ),
        LlmProvider(
            id="ollama",
            label="Ollama",
            langchain_provider="ollama",
            base_url_mode="optional",
            base_url_placeholder="http://localhost:11434",
            api_key_mode="unused",
            supports_forced_tool_choice=False,
        ),
    )
}

DEFAULT_PROVIDER = "openai_compatible"


def get_provider(provider_id: str) -> LlmProvider:
    provider = PROVIDERS.get(provider_id)
    if provider is None:
        raise ValueError(f"Unknown LLM provider: {provider_id!r}. Known: {sorted(PROVIDERS)}")
    return provider
