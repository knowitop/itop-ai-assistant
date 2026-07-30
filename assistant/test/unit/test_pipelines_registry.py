import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from itop_ai_assistant.config import IntakeConfig
from itop_ai_assistant.pipelines.registry import ModuleInfo, PipelineRegistry, build_registry


def _module(name: str = "test-module") -> ModuleInfo:
    return ModuleInfo(name=name, description="Test module")


def _settings(**intake_overrides) -> SimpleNamespace:
    return SimpleNamespace(intake=IntakeConfig(**intake_overrides))


class TestPipelineRegistry(unittest.TestCase):
    def test_resolve_registered_route(self):
        registry = PipelineRegistry()
        handler = AsyncMock()
        registry.register(_module(), {("UserRequest", "created"): handler})

        self.assertIs(registry.resolve("UserRequest", "created"), handler)

    def test_resolve_unknown_route_returns_none(self):
        registry = PipelineRegistry()
        registry.register(_module(), {("UserRequest", "created"): AsyncMock()})

        self.assertIsNone(registry.resolve("Change", "created"))
        self.assertIsNone(registry.resolve("UserRequest", "assigned"))

    def test_duplicate_module_raises(self):
        registry = PipelineRegistry()
        registry.register(_module("dup"), {})

        with self.assertRaises(ValueError):
            registry.register(_module("dup"), {})

    def test_conflicting_route_raises(self):
        registry = PipelineRegistry()
        registry.register(_module("a"), {("UserRequest", "created"): AsyncMock()})

        with self.assertRaises(ValueError) as ctx:
            registry.register(_module("b"), {("UserRequest", "created"): AsyncMock()})
        self.assertIn("UserRequest", str(ctx.exception))

    def test_modules_lists_registered(self):
        registry = PipelineRegistry()
        registry.register(_module("a"), {})
        registry.register(_module("b"), {})

        self.assertEqual([m.name for m in registry.modules], ["a", "b"])

    def test_resolve_entry_returns_module_name(self):
        registry = PipelineRegistry()
        handler = AsyncMock()
        registry.register(_module("my-module"), {("UserRequest", "created"): handler})

        entry = registry.resolve_entry("UserRequest", "created")

        self.assertEqual(entry, ("my-module", handler))
        self.assertIsNone(registry.resolve_entry("Change", "created"))

    def test_get_module(self):
        registry = PipelineRegistry()
        registry.register(_module("a"), {})

        self.assertEqual(registry.get_module("a").name, "a")
        self.assertIsNone(registry.get_module("nope"))


class TestBuildRegistry(unittest.TestCase):
    def test_default_settings_register_intake(self):
        registry = build_registry(_settings())

        for obj_class in ("UserRequest", "Incident"):
            for event in ("created", "user_commented", "assigned"):
                self.assertIsNotNone(registry.resolve(obj_class, event), f"{obj_class}/{event}")

        self.assertEqual([m.name for m in registry.modules], ["intake"])
        module = registry.get_module("intake")
        self.assertIs(module.config_model, IntakeConfig)
        self.assertIn("system", module.prompt_names)
        self.assertIsNotNone(module.validate_prompts)

    def test_disabled_intake_registers_nothing(self):
        registry = build_registry(_settings(enabled=False))

        self.assertEqual(registry.modules, [])
        self.assertIsNone(registry.resolve("UserRequest", "created"))

    def test_custom_class_list(self):
        registry = build_registry(_settings(classes=["UserRequest"]))

        self.assertIsNotNone(registry.resolve("UserRequest", "created"))
        self.assertIsNone(registry.resolve("Incident", "created"))

    def test_a_second_module_extends_the_map(self):
        registry = build_registry(_settings())

        registry.register(_module("other"), {("Change", "created"): AsyncMock()})

        self.assertEqual([m.name for m in registry.modules], ["intake", "other"])
        self.assertEqual(registry.resolve_entry("UserRequest", "created")[0], "intake")
        self.assertEqual(registry.resolve_entry("Change", "created")[0], "other")


if __name__ == "__main__":
    unittest.main()
