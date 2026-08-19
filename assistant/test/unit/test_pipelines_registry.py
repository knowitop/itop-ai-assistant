import importlib
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from itop_ai_assistant.agents.intake.config import IntakeConfig
from itop_ai_assistant.agents.selfcheck.config import SelfCheckConfig
from itop_ai_assistant.domain.identity import ObjectIdentity
from itop_ai_assistant.pipelines.registry import (
    ModuleInfo,
    RequestRoute,
    ScheduleRoute,
    TriggerRegistry,
    build_registry,
    discover_pipeline_modules,
)


def _module(name: str = "test-module") -> ModuleInfo:
    return ModuleInfo(name=name, description="Test module")


def _request(module: str = "test-module", action: str = "run") -> RequestRoute:
    return RequestRoute(
        action=action,
        module=module,
        input_model=ObjectIdentity,
        handler=AsyncMock(),
        subject_of=lambda ref: str(ref),
    )


def _schedule(module: str = "test-module", name: str = "tick", **overrides) -> ScheduleRoute:
    return ScheduleRoute(
        name=name,
        module=module,
        handler=AsyncMock(),
        interval_of=AsyncMock(return_value=60.0),
        **overrides,
    )


def _settings(intake_overrides=None, selfcheck_overrides=None) -> SimpleNamespace:
    configs = {
        "intake": IntakeConfig(**(intake_overrides or {})),
        "selfcheck": SelfCheckConfig(**(selfcheck_overrides or {})),
    }
    return SimpleNamespace(module_defaults=lambda name, model: configs[name])


class TestWebhookRoutes(unittest.TestCase):
    def test_resolve_registered_route(self):
        registry = TriggerRegistry()
        handler = AsyncMock()
        registry.register(_module("my-module"), webhooks={("UserRequest", "created"): handler})

        self.assertEqual(registry.resolve_webhook("UserRequest", "created"), ("my-module", handler))

    def test_resolve_unknown_route_returns_none(self):
        registry = TriggerRegistry()
        registry.register(_module(), webhooks={("UserRequest", "created"): AsyncMock()})

        self.assertIsNone(registry.resolve_webhook("Change", "created"))
        self.assertIsNone(registry.resolve_webhook("UserRequest", "assigned"))

    def test_conflicting_route_raises(self):
        registry = TriggerRegistry()
        registry.register(_module("a"), webhooks={("UserRequest", "created"): AsyncMock()})

        with self.assertRaises(ValueError) as ctx:
            registry.register(_module("b"), webhooks={("UserRequest", "created"): AsyncMock()})
        self.assertIn("UserRequest", str(ctx.exception))


class TestRequestRoutes(unittest.TestCase):
    def test_resolve_registered_request(self):
        registry = TriggerRegistry()
        route = _request()
        registry.register(_module(), requests=[route])

        self.assertIs(registry.resolve_request("test-module", "run"), route)

    def test_resolve_unknown_request_returns_none(self):
        registry = TriggerRegistry()
        registry.register(_module(), requests=[_request()])

        self.assertIsNone(registry.resolve_request("test-module", "nope"))
        self.assertIsNone(registry.resolve_request("other", "run"))

    def test_conflicting_request_raises(self):
        registry = TriggerRegistry()
        registry.register(_module("a"), requests=[_request(module="a")])

        with self.assertRaises(ValueError) as ctx:
            registry.register(_module("b"), requests=[_request(module="a")])
        self.assertIn("a/run", str(ctx.exception))

    def test_requests_for_module(self):
        registry = TriggerRegistry()
        registry.register(_module("a"), requests=[_request("a", "one"), _request("a", "two")])
        registry.register(_module("b"), requests=[_request("b", "one")])

        self.assertEqual([r.action for r in registry.requests_for("a")], ["one", "two"])
        self.assertEqual([r.action for r in registry.requests_for("b")], ["one"])
        self.assertEqual(registry.requests_for("nope"), [])


class TestScheduleRoutes(unittest.TestCase):
    def test_resolve_registered_schedule(self):
        registry = TriggerRegistry()
        route = _schedule()
        registry.register(_module(), schedules=[route])

        self.assertIs(registry.resolve_schedule("test-module", "tick"), route)
        self.assertEqual(registry.schedules, [route])

    def test_resolve_unknown_schedule_returns_none(self):
        registry = TriggerRegistry()
        registry.register(_module(), schedules=[_schedule()])

        self.assertIsNone(registry.resolve_schedule("test-module", "nope"))
        self.assertIsNone(registry.resolve_schedule("other", "tick"))

    def test_conflicting_schedule_raises(self):
        registry = TriggerRegistry()
        registry.register(_module("a"), schedules=[_schedule(module="a")])

        with self.assertRaises(ValueError) as ctx:
            registry.register(_module("b"), schedules=[_schedule(module="a")])
        self.assertIn("a/tick", str(ctx.exception))

    def test_schedules_for_module(self):
        registry = TriggerRegistry()
        registry.register(_module("a"), schedules=[_schedule("a", "one"), _schedule("a", "two")])
        registry.register(_module("b"), schedules=[_schedule("b", "one")])

        self.assertEqual([r.name for r in registry.schedules_for("a")], ["one", "two"])
        self.assertEqual([r.name for r in registry.schedules_for("b")], ["one"])
        self.assertEqual(registry.schedules_for("nope"), [])

    def test_label_defaults_to_module_and_name(self):
        self.assertEqual(_schedule("kb", "sweep").label, "kb/sweep")


class TestModules(unittest.TestCase):
    def test_duplicate_module_raises(self):
        registry = TriggerRegistry()
        registry.register(_module("dup"))

        with self.assertRaises(ValueError):
            registry.register(_module("dup"))

    def test_modules_lists_registered(self):
        registry = TriggerRegistry()
        registry.register(_module("a"))
        registry.register(_module("b"))

        self.assertEqual([m.name for m in registry.modules], ["a", "b"])

    def test_get_module(self):
        registry = TriggerRegistry()
        registry.register(_module("a"))

        self.assertEqual(registry.get_module("a").name, "a")
        self.assertIsNone(registry.get_module("nope"))


class TestBuildRegistry(unittest.TestCase):
    def test_default_settings_register_intake(self):
        registry = build_registry(_settings())

        for obj_class in ("UserRequest", "Incident"):
            for event in ("created", "user_commented", "assigned"):
                self.assertIsNotNone(registry.resolve_webhook(obj_class, event), f"{obj_class}/{event}")

        self.assertEqual([m.name for m in registry.modules], ["intake"])
        module = registry.get_module("intake")
        self.assertIs(module.config_model, IntakeConfig)
        self.assertIn("system", module.prompt_names)
        self.assertIsNotNone(module.validate_prompts)

    def test_intake_exposes_a_request_route(self):
        registry = build_registry(_settings())

        route = registry.resolve_request("intake", "process")

        self.assertIsNotNone(route)
        self.assertIs(route.input_model, ObjectIdentity)
        self.assertEqual(route.subject_of(ObjectIdentity(obj_class="Incident", obj_id="7")), "Incident::7")

    def test_disabled_intake_registers_nothing(self):
        registry = build_registry(_settings({"enabled": False}))

        self.assertEqual(registry.modules, [])
        self.assertIsNone(registry.resolve_webhook("UserRequest", "created"))
        self.assertIsNone(registry.resolve_request("intake", "process"))

    def test_custom_class_list(self):
        registry = build_registry(_settings({"classes": ["UserRequest"]}))

        self.assertIsNotNone(registry.resolve_webhook("UserRequest", "created"))
        self.assertIsNone(registry.resolve_webhook("Incident", "created"))

    def test_a_second_module_extends_the_map(self):
        registry = build_registry(_settings())

        registry.register(_module("other"), webhooks={("Change", "created"): AsyncMock()})

        self.assertEqual([m.name for m in registry.modules], ["intake", "other"])
        self.assertEqual(registry.resolve_webhook("UserRequest", "created")[0], "intake")
        self.assertEqual(registry.resolve_webhook("Change", "created")[0], "other")


class TestDiscoverPipelineModules(unittest.TestCase):
    """`intake`/`selfcheck` (via `TestBuildRegistry` above) are one
    implementation of "a subpackage with a `pipeline.py`"; a throwaway
    fixture package built here on disk is the second — same rule as the
    request/schedule routes, `.claude/rules/testing.md`. TASK-046 scope
    item 3: a one-off module-package registers without touching
    `pipelines/registry.py`.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._root = Path(self._tmp.name)
        sys.path.insert(0, self._tmp.name)
        self.addCleanup(sys.path.remove, self._tmp.name)
        self.addCleanup(self._forget_fixture_modules)

    def _forget_fixture_modules(self):
        for name in [n for n in sys.modules if n == "fixture_agents" or n.startswith("fixture_agents.")]:
            del sys.modules[name]

    def _package(self, subpackages: dict[str, bool]):
        """`subpackages` maps subpackage name to whether it gets a `pipeline.py`."""
        package_dir = self._root / "fixture_agents"
        package_dir.mkdir()
        (package_dir / "__init__.py").write_text("")
        for name, has_pipeline in subpackages.items():
            module_dir = package_dir / name
            module_dir.mkdir()
            (module_dir / "__init__.py").write_text("")
            if has_pipeline:
                (module_dir / "pipeline.py").write_text("def register(registry, settings):\n    pass\n")
        return importlib.import_module("fixture_agents")

    def test_finds_a_throwaway_module_without_naming_it(self):
        package = self._package({"acme": True})

        modules = discover_pipeline_modules(package)

        self.assertEqual([m.__name__ for m in modules], ["fixture_agents.acme.pipeline"])

    def test_a_subpackage_without_pipeline_py_is_skipped(self):
        package = self._package({"acme": True, "not_a_module": False})

        modules = discover_pipeline_modules(package)

        self.assertEqual([m.__name__ for m in modules], ["fixture_agents.acme.pipeline"])


if __name__ == "__main__":
    unittest.main()
