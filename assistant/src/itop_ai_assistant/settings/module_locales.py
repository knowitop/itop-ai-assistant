"""Translations of a module's admin-form texts, shipped with the module.

The admin form is built from `GET /api/config/{module}/schema` and knows no
module by name (ADR-025), so every text it shows — field labels, descriptions,
section headings, what an action does — arrives from the backend. Their
English is the `title`/`description` of the config model itself; their
translations live next to that model, in `<module>/locales/<lang>.json`, and
are applied to the schema on the way out (ADR-030).

Why next to the module and not in the SPA's locale files: a module found by a
package scan (ADR-023) must be translatable without touching `ui/`, and a text
kept a directory away from the field it names is the copy that goes stale.

Nothing here is required. A module without a `locales/` directory, a language
without a file, a field without a key — each falls back to the English the
schema already carries, so a translation can be partial and stay useful.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: What may become a file name. The language reaches us from a query string,
#: so this is a path guard first and a format check second.
_LANG = re.compile(r"^[a-z]{2,3}(-[a-z]{2,4})?$", re.IGNORECASE)

#: Keys a translation file may use, checked to catch a typo in a file nobody
#: validates at boot — a misspelled section simply would not translate.
_SECTIONS = frozenset({"description", "groups", "fields", "actions", "schedules"})

#: What a field entry may translate — the two standard JSON Schema keys.
_TEXTS = ("title", "description")


def normalize_language(lang: str | None) -> str | None:
    """`"ru-RU"` → `"ru"`; anything that is not a language tag → `None`.

    A region is dropped rather than looked up: we translate a language, and
    `ru-RU` and `ru-KZ` would name the same file anyway.
    """
    if not lang or not _LANG.match(lang):
        return None
    return lang.split("-")[0].lower()


@dataclass(frozen=True)
class ModuleTranslation:
    """One module's texts in one language, or nothing at all.

    `EMPTY` is a full member of the type rather than a `None` every caller
    would have to check: an untranslated module and a translated one go
    through the same code path, and the untranslated one hands back what it
    was given.
    """

    description: str | None = None
    groups: Mapping[str, str] = field(default_factory=dict)
    fields: Mapping[str, Mapping[str, str]] = field(default_factory=dict)
    actions: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    schedules: Mapping[str, Mapping[str, str]] = field(default_factory=dict)

    def module_description(self, default: str) -> str:
        return self.description or default

    def action_summary(self, action: str, default: str) -> str:
        return str(self.actions.get(action, {}).get("summary") or default)

    def schedule_summary(self, name: str, default: str) -> str:
        return str(self.schedules.get(name, {}).get("summary") or default)

    def config_schema(self, schema: dict[str, Any]) -> dict[str, Any]:
        """The config schema with its field texts and group headings translated."""
        return self._schema(schema, self.fields)

    def action_schema(self, action: str, schema: dict[str, Any]) -> dict[str, Any]:
        """The input schema of one request action, translated the same way."""
        fields = self.actions.get(action, {}).get("fields", {})
        return self._schema(schema, fields)

    def _schema(self, schema: dict[str, Any], fields: Mapping[str, Any]) -> dict[str, Any]:
        properties = schema.get("properties")
        if not properties:
            return schema
        translated = {name: self._property(prop, fields.get(name, {})) for name, prop in properties.items()}
        return {**schema, "properties": translated}

    def _property(self, prop: dict[str, Any], texts: Mapping[str, str]) -> dict[str, Any]:
        # The group heading is written for a human in the schema and is its own
        # key in the translation, so several fields naming one section are
        # translated once.
        group = prop.get("x-group")
        replacements = {key: texts[key] for key in _TEXTS if texts.get(key)}
        if group and group in self.groups:
            replacements["x-group"] = self.groups[group]
        return {**prop, **replacements} if replacements else prop


EMPTY = ModuleTranslation()


def load_translation(locales_dir: Path | None, lang: str | None) -> ModuleTranslation:
    """Read `<locales_dir>/<lang>.json`, or `EMPTY` if there is nothing to read.

    Read on every call rather than cached: this happens once per opened form,
    and an edited file applying without a restart is worth more here than the
    read it saves.

    A malformed or unreadable file is a warning, never an error: the form has
    working English without it, and refusing to serve the schema over a broken
    translation would take the module's settings away entirely.
    """
    code = normalize_language(lang)
    if locales_dir is None or code is None:
        return EMPTY
    path = locales_dir / f"{code}.json"
    if not path.is_file():
        return EMPTY
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        logger.warning(f"Ignoring translation {path}: {e}")
        return EMPTY
    if not isinstance(raw, dict):
        logger.warning(f"Ignoring translation {path}: expected an object, got {type(raw).__name__}")
        return EMPTY
    unknown = sorted(set(raw) - _SECTIONS)
    if unknown:
        logger.warning(f"Translation {path} has unknown sections, ignored: {unknown}")
    return ModuleTranslation(
        description=raw.get("description"),
        groups=raw.get("groups", {}),
        fields=raw.get("fields", {}),
        actions=raw.get("actions", {}),
        schedules=raw.get("schedules", {}),
    )
