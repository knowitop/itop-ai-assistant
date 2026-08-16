"""The smoke module, and through it the module contract itself.

Selfcheck exists to prove that a business module needs nothing from the core
beyond its registration, so these tests are as much about the platform as
about the module: the same handler reached from two trigger kinds, prompts
read from the package, iTop reached only through the repository.
"""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import fakeredis.aioredis
from langchain_core.messages import AIMessage

from itop_ai_assistant.agents.selfcheck.config import SelfCheckConfig
from itop_ai_assistant.agents.selfcheck.pipeline import (
    MODULE,
    SelfCheckInput,
    handle_request,
    register,
    run_selfcheck,
)
from itop_ai_assistant.agents.selfcheck.prompts import build_selfcheck_prompts
from itop_ai_assistant.config import ItopConfig, LlmConfig
from itop_ai_assistant.domain.catalog import Service
from itop_ai_assistant.pipelines.context import RunContext
from itop_ai_assistant.pipelines.registry import TriggerRegistry
from itop_ai_assistant.settings.prompt_store import PACKAGED_PROMPTS_DIR, FilePromptStore
from itop_ai_assistant.state.journal import RunJournal

_ITOP = ItopConfig(url="https://itop.example", token="t")
_LLM = LlmConfig(base_url="http://llm/v1", model="test-model")


def _settings(**overrides) -> SimpleNamespace:
    cfg = SelfCheckConfig(**{"enabled": True, **overrides})
    return SimpleNamespace(module_defaults=lambda name, model: cfg)


def _deps(*, itop=_ITOP, llm=_LLM, selfcheck=None, services=2) -> MagicMock:
    sections = {"itop": itop, "llm": llm, MODULE: selfcheck or SelfCheckConfig(enabled=True)}
    deps = MagicMock()
    deps.config_store.get = AsyncMock(side_effect=lambda name, model: sections[name])
    deps.prompt_store = FilePromptStore(PACKAGED_PROMPTS_DIR)
    deps.journal = RunJournal(fakeredis.aioredis.FakeRedis(decode_responses=True))
    catalog = MagicMock()
    catalog.find_services = AsyncMock(
        return_value=[Service(id=str(i), name=f"svc-{i}", description="") for i in range(services)]
    )
    deps.itop.for_principal = AsyncMock(return_value=SimpleNamespace(catalog_repo=catalog))
    return deps


def _llm_mock(answer: str = "I am the assistant. 2 services.") -> MagicMock:
    llm = MagicMock()
    llm.ainvoke = AsyncMock(return_value=AIMessage(content=answer))
    return llm


class SelfCheckTestCase(unittest.IsolatedAsyncioTestCase):
    async def _run(self, deps, llm=None):
        self.llm = llm or _llm_mock()
        self.pid = uuid4()
        await deps.journal.start(self.pid, subject=MODULE, event="tick", module=MODULE, kind="schedule")
        with patch("itop_ai_assistant.agents.selfcheck.pipeline.create_llm", return_value=self.llm):
            return await run_selfcheck(RunContext(processing_id=self.pid, module=MODULE), deps)

    async def _steps(self, deps) -> dict[str, str]:
        run = await deps.journal.get(self.pid)
        return {step.node: step.detail for step in run.steps}


class TestRegistration(unittest.TestCase):
    def test_registers_both_trigger_kinds(self):
        registry = TriggerRegistry()
        register(registry, _settings())

        schedule = registry.resolve_schedule(MODULE, "tick")
        request = registry.resolve_request(MODULE, "run")
        self.assertIsNotNone(schedule)
        self.assertIsNotNone(request)
        # The same work behind both — only the delivery of the answer differs
        self.assertIs(schedule.handler, run_selfcheck)
        self.assertIs(request.handler, handle_request)

    def test_schedule_subject_is_not_a_ticket(self):
        registry = TriggerRegistry()
        register(registry, _settings())

        self.assertEqual(registry.resolve_schedule(MODULE, "tick").subject, MODULE)

    def test_disabled_registers_nothing(self):
        registry = TriggerRegistry()
        register(registry, _settings(enabled=False))

        self.assertEqual(registry.modules, [])
        self.assertEqual(registry.schedules, [])

    def test_module_metadata_exposes_config_and_prompts(self):
        registry = TriggerRegistry()
        register(registry, _settings())

        info = registry.get_module(MODULE)
        self.assertIs(info.config_model, SelfCheckConfig)
        self.assertEqual(info.prompt_names, ("greeting",))
        self.assertIsNotNone(info.validate_prompts)


class TestPrompts(unittest.IsolatedAsyncioTestCase):
    async def test_packaged_prompt_validates(self):
        """The same call the lifespan makes — a broken template fails the boot,
        not a live run."""
        raw = await FilePromptStore(PACKAGED_PROMPTS_DIR).get(MODULE)

        self.assertIn("{services}", build_selfcheck_prompts(raw).greeting)

    async def test_unknown_placeholder_is_rejected(self):
        with self.assertRaises(ValueError):
            build_selfcheck_prompts({"greeting": "hi {nope}"})

    async def test_missing_template_is_rejected(self):
        with self.assertRaises(ValueError):
            build_selfcheck_prompts({})


class TestRun(SelfCheckTestCase):
    async def test_reads_itop_and_the_model_and_journals_both(self):
        deps = _deps(services=3)

        outcome = await self._run(deps)

        self.assertEqual(outcome.status, "done")
        self.assertIn("3 services", outcome.detail)
        steps = await self._steps(deps)
        self.assertIn("3 services", steps["itop"])
        self.assertIn("I am the assistant", steps["llm"])

    async def test_probe_oql_comes_from_the_config(self):
        deps = _deps(selfcheck=SelfCheckConfig(enabled=True, probe_oql="SELECT Service WHERE id = 1"))

        await self._run(deps)

        deps.itop.for_principal.return_value.catalog_repo.find_services.assert_awaited_once_with(
            "SELECT Service WHERE id = 1"
        )

    async def test_the_catalog_size_reaches_the_prompt(self):
        deps = _deps(services=5)

        await self._run(deps)

        prompt = self.llm.ainvoke.await_args.args[0][0].content
        self.assertIn("5 services", prompt)

    async def test_thinking_blocks_are_stripped(self):
        deps = _deps()

        await self._run(deps, llm=_llm_mock("<think>hmm</think>Hello."))

        self.assertEqual((await self._steps(deps))["llm"], "Hello.")

    async def test_skipped_when_setup_is_incomplete(self):
        deps = _deps(itop=ItopConfig())

        outcome = await self._run(deps)

        self.assertEqual(outcome.status, "skipped")
        self.assertIn("setup incomplete", outcome.detail)
        deps.itop.for_principal.assert_not_awaited()
        self.assertIn("guard", await self._steps(deps))

    async def test_request_trigger_runs_the_same_work(self):
        deps = _deps()
        pid = uuid4()
        await deps.journal.start(pid, subject=MODULE, event="run", module=MODULE, kind="request")

        with patch("itop_ai_assistant.agents.selfcheck.pipeline.create_llm", return_value=_llm_mock()):
            outcome = await handle_request(SelfCheckInput(), RunContext(processing_id=pid, module=MODULE), deps)

        self.assertEqual(outcome.status, "done")
        run = await deps.journal.get(pid)
        self.assertEqual([step.node for step in run.steps], ["itop", "llm"])


if __name__ == "__main__":
    unittest.main()
