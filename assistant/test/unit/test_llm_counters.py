"""What the model handler counts, and what it refuses to invent.

The endpoint an installation runs is not ours: a self-hosted server may report
no token usage at all, and a missing count has to stay a zero rather than
become a guess in somebody's dashboard.
"""

import unittest
from datetime import UTC, datetime
from uuid import uuid4

import fakeredis
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, LLMResult

from itop_ai_assistant.core.llm_counters import LlmCallCounter
from itop_ai_assistant.state.counters import Counter, DailyCounters


def _result(*usages: dict | None) -> LLMResult:
    """One inner list per prompt — the shape langchain hands a batched call."""
    return LLMResult(
        generations=[
            [
                ChatGeneration(
                    message=AIMessage(content="ok", usage_metadata=usage) if usage else AIMessage(content="ok")
                )
            ]
            for usage in usages
        ]
    )


_USAGE = {"input_tokens": 120, "output_tokens": 30, "total_tokens": 150}


class LlmCounterTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.counters = DailyCounters(fakeredis.aioredis.FakeRedis(decode_responses=True))
        self.handler = LlmCallCounter(self.counters)

    async def _read(self) -> dict[Counter, int]:
        return await self.counters.read(datetime.now(UTC).date())


class TestSuccessfulCall(LlmCounterTestCase):
    async def test_a_call_and_its_tokens_are_counted(self):
        await self.handler.on_llm_end(_result(_USAGE), run_id=uuid4())

        counters = await self._read()

        self.assertEqual(1, counters[Counter.LLM_CALLS])
        self.assertEqual(120, counters[Counter.LLM_TOKENS_IN])
        self.assertEqual(30, counters[Counter.LLM_TOKENS_OUT])

    async def test_an_endpoint_that_reports_no_usage_still_counts_the_call(self):
        await self.handler.on_llm_end(_result(None), run_id=uuid4())

        counters = await self._read()

        self.assertEqual(1, counters[Counter.LLM_CALLS])
        self.assertEqual(0, counters[Counter.LLM_TOKENS_IN])
        self.assertEqual(0, counters[Counter.LLM_TOKENS_OUT])


class TestBatchedCall(LlmCounterTestCase):
    async def test_every_prompt_in_a_batch_is_counted(self):
        """The handler fires once for the whole batch: reading only the first
        prompt's usage would under-report the day by however many rode along."""
        await self.handler.on_llm_end(_result(_USAGE, _USAGE, _USAGE), run_id=uuid4())

        counters = await self._read()

        self.assertEqual(3, counters[Counter.LLM_CALLS])
        self.assertEqual(360, counters[Counter.LLM_TOKENS_IN])
        self.assertEqual(90, counters[Counter.LLM_TOKENS_OUT])


class TestFailedCall(LlmCounterTestCase):
    async def test_a_failure_is_counted_apart_from_the_call(self):
        await self.handler.on_llm_error(TimeoutError("endpoint is gone"), run_id=uuid4())

        counters = await self._read()

        self.assertEqual(1, counters[Counter.LLM_FAILURES])
        self.assertEqual(0, counters[Counter.LLM_CALLS])


if __name__ == "__main__":
    unittest.main()
