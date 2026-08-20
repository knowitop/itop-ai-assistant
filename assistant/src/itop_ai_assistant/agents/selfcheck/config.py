"""Selfcheck's own runtime-editable config section.

Resolved through `Settings.module_defaults` / `RedisConfigStore`, not a field
of `Settings` — see `settings/config_store.py`.
"""

from pydantic import BaseModel, Field

from itop_ai_assistant.settings.ui_hints import ui


class SelfCheckConfig(BaseModel):
    """The smoke module: it touches every seam and changes nothing.

    Its job is to prove the platform's own contracts on a live deployment —
    a config section, a prompt file, an LLM call, an iTop read and a journal
    entry, reached through the same trigger registry every business module
    uses. It writes nothing anywhere, which is why it is safe to schedule.
    """

    # Read at startup like intake's: off by default, because nobody wants a
    # fresh deployment calling a model on a timer for no business reason
    enabled: bool = Field(
        default=False,
        title="Enabled",
        description="Run the smoke check on a timer. Read at startup: switching it needs a restart.",
    )
    interval_seconds: int = Field(
        default=900,
        gt=0,
        title="Run every, seconds",
        description="Re-read before every tick, so a change here applies without a restart.",
    )
    # Cheapest read that proves the connection and the credentials at once
    probe_oql: str = Field(
        default="SELECT Service",
        title="Probe query",
        description="The OQL the check runs against iTop. Its result is discarded; only the call has to succeed.",
        json_schema_extra=ui(widget="oql"),
    )
    model: str | None = Field(
        default=None,
        title="Model override",
        description="Leave empty to use the global model.",
    )
