"""Selfcheck's own runtime-editable config section.

Resolved through `Settings.module_defaults` / `RedisConfigStore`, not a field
of `Settings` — see `settings/config_store.py`.
"""

from pydantic import BaseModel, Field


class SelfCheckConfig(BaseModel):
    """The smoke module: it touches every seam and changes nothing.

    Its job is to prove the platform's own contracts on a live deployment —
    a config section, a prompt file, an LLM call, an iTop read and a journal
    entry, reached through the same trigger registry every business module
    uses. It writes nothing anywhere, which is why it is safe to schedule.
    """

    # Read at startup like intake's: off by default, because nobody wants a
    # fresh deployment calling a model on a timer for no business reason
    enabled: bool = False
    interval_seconds: int = Field(default=900, gt=0)
    # Cheapest read that proves the connection and the credentials at once
    probe_oql: str = "SELECT Service"
    model: str | None = None
