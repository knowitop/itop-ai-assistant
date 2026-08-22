"""Tracing off is a promise about imports, not just about behaviour.

The extra (`uv sync --extra tracing`) is optional, and both tests here run in
the environment that proves it: the packages blocked outright, as they are
absent in CI. What is checked is that the disabled branch never reaches for
them, and that the enabled one survives their absence instead of taking the
service down with it.
"""

import os
import sys
import unittest
from contextlib import contextmanager
from importlib.abc import MetaPathFinder
from unittest.mock import patch

from fastapi.testclient import TestClient

from itop_ai_assistant.config import Settings
from itop_ai_assistant.core.tracing import NullRunTracer, setup_tracing
from itop_ai_assistant.main import app

#: Exactly what the extra installs. `opentelemetry` is safe to block: nothing
#: else in the dependency tree imports it (checked for langchain, langgraph and
#: langsmith). Should a transitive dependency ever start to, narrow this to
#: `openinference` plus "core.tracing_otel is not in sys.modules" — the subject
#: is that the disabled branch did not run, not who owns the package.
_TRACING_PACKAGES = ("opentelemetry", "openinference")


class _BlockedImport(MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in _TRACING_PACKAGES:
            raise ImportError(f"{fullname} is not installed")
        return None


@contextmanager
def tracing_packages_absent():
    already_loaded = [name for name in sys.modules if name.split(".")[0] in _TRACING_PACKAGES]
    for name in already_loaded:
        del sys.modules[name]
    sys.modules.pop("itop_ai_assistant.core.tracing_otel", None)
    finder = _BlockedImport()
    sys.meta_path.insert(0, finder)
    try:
        yield
    finally:
        sys.meta_path.remove(finder)


class TestTracingOff(unittest.TestCase):
    def test_app_starts_where_the_tracing_packages_do_not_exist(self):
        with tracing_packages_absent():
            with TestClient(app) as client:
                tracer = client.app.state.deps.tracer

            self.assertIsInstance(tracer, NullRunTracer)
            with tracer.run_span(
                "pid",
                subject="UserRequest::1",
                event="created",
                module="intake",
                kind="webhook",
                principal="service",
                dry_run=False,
            ):
                pass
            loaded = [name for name in sys.modules if name.split(".")[0] in _TRACING_PACKAGES]
            self.assertEqual([], loaded)

    def test_enabled_without_the_packages_logs_and_carries_on(self):
        # Through the environment, not through `Settings(...)`: init arguments
        # are deliberately not a settings source here (config.py).
        with patch.dict(os.environ, {"TRACING_ENABLED": "true"}), tracing_packages_absent():
            with self.assertLogs("itop_ai_assistant.core.tracing", level="ERROR") as logs:
                tracer = setup_tracing(Settings())

        self.assertIsInstance(tracer, NullRunTracer)
        self.assertIn("uv sync --extra tracing", logs.output[0])
