from typing import Protocol

from config import EnrichmentConfig, Settings


class ConfigStore(Protocol):
    """Source of runtime business configuration.

    Read once per processing run so a single run always sees a consistent
    snapshot. Static for now; a UI-editable store (e.g. Redis-backed) can
    replace it without touching the processing code.
    """

    async def get_enrichment(self) -> EnrichmentConfig: ...


class StaticConfigStore:
    """Serves business config from application settings (env/yaml/defaults)."""

    def __init__(self, settings: Settings):
        self._settings = settings

    async def get_enrichment(self) -> EnrichmentConfig:
        return self._settings.enrichment
