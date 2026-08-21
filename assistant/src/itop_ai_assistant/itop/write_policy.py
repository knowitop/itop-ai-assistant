"""Whether what a run does reaches iTop at all — the dry run (REQ-006).

Lives next to the connection because that is what the switch is about: the
unit of the mode is a write to iTop, not an action of a module. Everything
else keeps working and is meant to — the catalogue is read, the model is
called, the vector index is searched and kept up to date, the run journal
records every step.

The ban itself is not here. It is a view of the client
(`itop_client.Itop.read_only`), handed out by `ItopRepositories.for_principal`,
which asks this class rather than taking a caller's word for it: a module that
knows nothing about the mode must not be able to write even by mistake, and one
that does know must not be able to ask for an exception (REQ R2).
"""

from itop_ai_assistant.config import PlatformConfig
from itop_ai_assistant.settings.config_store import ConfigStore

SECTION = "platform"


class WritePolicy:
    """Owner of section `platform`, asked once per repository set.

    Read on every call and never cached: switching the mode applies from the
    next run, with no restart and nothing to invalidate.

    No `module` argument today, and that is the whole shape of the extension
    REQ R5 asks to leave room for: a per-module switch changes where the
    answer comes from — this signature and the module's own config section —
    while the point the ban is enforced at stays exactly where it is.
    """

    def __init__(self, config_store: ConfigStore):
        self._config_store = config_store

    async def dry_run(self) -> bool:
        """True when nothing this installation does may reach iTop as a change."""
        cfg = await self._config_store.get(SECTION, PlatformConfig)
        return cfg.dry_run
