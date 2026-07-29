"""Unit tests run against stock defaults, never the developer's working copy.

`Settings` reads `assistant/config.yaml` and `assistant/.env`, and the tests
that build the app (`TestClient(app)` → lifespan → `build_registry`) inherit
whatever is configured there. A local tweak — a module switched off, a
narrowed class list — then fails a dozen tests that have nothing to do with it.

Both file sources are pointed at a path that does not exist. Environment
variables keep working, so tests that set them on purpose (`test_config.py`)
are unaffected.
"""

import pytest

from config import Settings, get_settings


@pytest.fixture(autouse=True)
def stock_settings(monkeypatch, tmp_path):
    absent = str(tmp_path / "no-such-file")
    monkeypatch.setitem(Settings.model_config, "yaml_file", absent)
    monkeypatch.setitem(Settings.model_config, "env_file", absent)
    # main.py builds a Settings at import time, long before the patch above
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
