"""Model calls and tokens, counted once for every module that calls a model.

Hangs off the factory (`core/deps.py::create_llm`), not off its consumers: the
intake agent and the selfcheck pipeline never learn that anything is counted,
and a module added later inherits the counting by construction — the same
reason the tracing instrumentation is installed above them (ADR-029) rather
than in each of them.

Not the global instrumentation ADR-029 uses, though, and the difference is
technical rather than a matter of taste. Tracing installs one process-wide
`LangChainInstrumentor`; the equivalent for a plain handler is a `ContextVar`
registered with `register_configure_hook`, which would have to be set during
the lifespan — a context that does not reach the tasks serving requests. A
handler passed to the model reaches every call the model makes, including the
ones inside the agent loop, and needs no globals to do it.

The wizard's model probe (`admin/setup.py`) calls the factory without a handler
and is deliberately uncounted: two probes fire per click on "Test", and an
installation still choosing an endpoint would otherwise look like one doing
work. What is counted is work done for tickets.
"""

import logging
from typing import Any
from uuid import UUID

from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.outputs import LLMResult

from itop_ai_assistant.state.counters import Counter, DailyCounters

logger = logging.getLogger(__name__)


class LlmCallCounter(AsyncCallbackHandler):
    """One model call in, four counters out: the call, its tokens, or its failure.

    LangChain swallows what a handler raises (`raise_error` is False) and
    `DailyCounters.bump` is non-fatal in its own right — two reasons a counter
    cannot cost a run, which is what makes it safe to sit in the agent loop.
    """

    def __init__(self, counters: DailyCounters):
        self._counters = counters

    async def on_llm_end(self, response: LLMResult, *, run_id: UUID, **kwargs: Any) -> None:
        """The call and its tokens in one round trip: this fires on every turn
        of the agent loop, and three transactions where one does would put the
        counting on the run's critical path.

        A batched call counts as the calls it carried — `generations` holds one
        list per prompt, and the handler fires once for the batch.
        """
        tokens_in, tokens_out = _usage(response)
        await self._counters.bump_many(
            {
                Counter.LLM_CALLS: len(response.generations),
                Counter.LLM_TOKENS_IN: tokens_in,
                Counter.LLM_TOKENS_OUT: tokens_out,
            }
        )

    async def on_llm_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        """Counted apart from the call, not as one: `llm_calls` answers "how
        much work", `llm_failures` answers "does the endpoint hold up" — the
        second is the one an installation nobody can log into will not report
        any other way."""
        await self._counters.bump(Counter.LLM_FAILURES)

    async def on_chat_model_start(self, *args: Any, **kwargs: Any) -> None:
        """Declared so the callback manager treats this handler as chat-aware.

        Without it langchain falls back to `on_llm_start`, which is a
        deprecation path, not a behaviour we want to depend on. Nothing is
        counted at the start — a call that never returns is not a call.
        """


def _usage(response: LLMResult) -> tuple[int, int]:
    """Input and output tokens of one response, zero where the endpoint said nothing.

    Read off the message's `usage_metadata`, the shape every chat model
    normalizes to. Self-hosted endpoints do not always report usage, and a
    missing count is a zero rather than a guess: an installation running a
    local model would otherwise report invented tokens.

    Summed over `generations`, which holds one list *per prompt*: this handler
    fires once for a batched call, and reading only the first prompt's usage
    would under-report the day by however many prompts rode along.
    """
    tokens_in = tokens_out = 0
    for generations in response.generations:
        for generation in generations:
            usage = getattr(getattr(generation, "message", None), "usage_metadata", None)
            if usage:
                tokens_in += int(usage.get("input_tokens", 0))
                tokens_out += int(usage.get("output_tokens", 0))
                # One usage report per prompt; the rest of the list is the
                # same call answered `n` times.
                break
    return tokens_in, tokens_out
