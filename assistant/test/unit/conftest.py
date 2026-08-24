"""Unit tests run against stock defaults, and never act on their own.

`Settings` reads `assistant/config.yaml` and `assistant/.env`, and the tests
that build the app (`TestClient(app)` → lifespan → `build_registry`) inherit
whatever is configured there. A local tweak — a module switched off, a
narrowed class list — then fails a dozen tests that have nothing to do with it.

Both file sources are pointed at a path that does not exist. Environment
variables keep working, so tests that set them on purpose (`test_config.py`)
are unaffected.
"""

import pytest

from itop_ai_assistant.config import Settings, get_settings
from itop_ai_assistant.pipelines.scheduler import PeriodicTasks


@pytest.fixture(autouse=True)
def stock_settings(monkeypatch, tmp_path):
    absent = str(tmp_path / "no-such-file")
    monkeypatch.setitem(Settings.model_config, "yaml_file", absent)
    monkeypatch.setitem(Settings.model_config, "env_file", absent)
    # main.py builds a Settings at import time, long before the patch above
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def no_background_loops(monkeypatch):
    """A test that builds the app gets its scheduler empty.

    `TestClient(app)` runs the real lifespan, and the loops it starts hold the
    real `AppDeps` — pointed at whatever `REDIS_URL` says, which on a developer
    machine is usually the stand. Harmless while every loop only reads, and not
    harmless since one of them sends: the telemetry tick fires immediately on
    start, and a stand with telemetry switched on would have `pytest` report
    its day to the real receiver.

    `PeriodicTasks` itself is left alone — `test_scheduler.py` tests the class
    and needs it whole.
    """
    monkeypatch.setattr("itop_ai_assistant.main.build_background_tasks", lambda deps, registry: PeriodicTasks())
