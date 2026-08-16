"""Assembly for one intake run.

Everything `IntakeRun.body` (`run.py`) needs before it can construct
`IntakeAgentRun` and stream it — kept apart from the scenario itself so
"what intake does with a ticket" reads without the wiring in front of it
(TASK-026).
"""

import logging
from dataclasses import dataclass

from langchain_core.messages import BaseMessage
from langgraph.graph.state import CompiledStateGraph

from itop_ai_assistant.config import EmbeddingsConfig, LlmConfig, VectorConfig
from itop_ai_assistant.core.deps import create_llm
from itop_ai_assistant.domain.ticket import Ticket
from itop_ai_assistant.pipelines.context import RunContext
from itop_ai_assistant.pipelines.ports import RunDeps
from itop_ai_assistant.repositories.sets import RepositorySet
from itop_ai_assistant.vector import EmbeddingsClient, SimilarSearch

from .agent import build_intake_agent
from .config import IntakeConfig
from .context import IntakeContext
from .prompt import build_initial_messages
from .prompts import build_intake_prompts
from .tools import tools_for

logger = logging.getLogger(__name__)


@dataclass
class ComposedRun:
    """What `body()` needs to construct `IntakeAgentRun` and stream it.

    `embedder`'s lifecycle (`aclose()`) stays the caller's: it must close
    whether the stream succeeds, fails or is never started, and that is a
    `body()`-level concern, not this module's.
    """

    agent: CompiledStateGraph
    context: IntakeContext
    messages: list[BaseMessage]
    think_tags: tuple[str, ...]
    embedder: EmbeddingsClient | None


async def assemble(ticket: Ticket, ai_name: str, run: RunContext, deps: RunDeps, repos: RepositorySet) -> ComposedRun:
    cfg = await deps.config_store.get("intake", IntakeConfig)
    llm_cfg = await deps.config_store.get("llm", LlmConfig)
    prompts = build_intake_prompts(await deps.prompt_store.get("intake"))
    searchable = await _searchable(deps)
    # The run's own principal, not the service account by default: what a
    # search may return is decided by whoever this run acts as (TASK-032). For
    # a webhook-driven intake run the two are the same thing — the point is
    # that nothing here gets to choose.
    similar = (
        SimilarSearch(deps.vector_store, searchable.embedder, deps.itop, searchable.vector_cfg)
        if searchable is not None
        else None
    )
    context = IntakeContext(
        processing_id=run.processing_id,
        principal=run.principal,
        ticket=ticket,
        ticket_repo=repos.ticket_repo,
        catalog_repo=repos.catalog_repo,
        state_manager=deps.state_manager,
        intake=cfg,
        ai_name=ai_name,
        similar=similar,
    )
    agent = build_intake_agent(
        create_llm(llm_cfg, cfg.model),
        cfg,
        tools_for(ticket, similar=similar is not None),
        # Nothing here can deliver prose, so force a tool call wherever the
        # endpoint accepts being forced
        force_tool_choice=llm_cfg.endpoint_forces_tool_choice,
    )
    messages = await build_initial_messages(context, prompts)
    return ComposedRun(
        agent=agent,
        context=context,
        messages=messages,
        think_tags=tuple(llm_cfg.think_tags),
        embedder=searchable.embedder if searchable is not None else None,
    )


@dataclass(frozen=True)
class _Searchable:
    """What a deployment that can search hands the search: a client of its own
    and the vector configuration the family list is built from."""

    embedder: EmbeddingsClient
    vector_cfg: VectorConfig


async def _searchable(deps: RunDeps) -> _Searchable | None:
    """The ingredients of a search, or None when this deployment cannot search.

    Three conditions, all necessary: a vector store to search, indexing
    switched on — a stale index quoted as current is worse than no
    references at all — and an embeddings endpoint to turn the ticket into
    a query with. The client is per run and closed by the caller.

    `vector_cfg` travels back out because it is already read here and the
    search needs it too; reading the section twice per run would be the only
    alternative.
    """
    if not deps.vector_store.configured:
        return None
    vector_cfg = await deps.config_store.get("vector", VectorConfig)
    emb_cfg = await deps.config_store.get("embeddings", EmbeddingsConfig)
    if not vector_cfg.enabled or not emb_cfg.base_url or not emb_cfg.model:
        return None
    return _Searchable(embedder=EmbeddingsClient(emb_cfg), vector_cfg=vector_cfg)
