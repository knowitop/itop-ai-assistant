"""Hints a config field carries into its JSON schema for the admin form.

The admin UI builds every module form from `GET /api/config/{module}/schema`
and knows no module by name, so anything it needs to render a field well has to
travel in the schema itself (ADR-025). JSON Schema covers most of it —
`title`, `description`, `default`, the constraint keywords, `enum` — but has no
word for which section a field belongs to, which switch owns that section,
whether a string is one line or many, and what to keep out of sight by default.
Those go under `x-` keys, which pydantic passes through untouched and any other
consumer of the schema ignores.

Declaring them through this helper rather than as literal dicts keeps the set
of keys in one place: a new kind of hint is a change here and in the UI
together, never a key invented in a module's config.
"""

from typing import Any, Literal

# `oql` is `textarea` plus a monospace font — the distinction is about the kind
# of value, not the widget, so the UI stays free to render both the same way.
Widget = Literal["oql", "textarea"]


def ui(
    *,
    group: str | None = None,
    toggle: bool = False,
    widget: Widget | None = None,
    advanced: bool = False,
) -> dict[str, Any]:
    """Build the `json_schema_extra` of one config field.

    `group` is the section's heading as an administrator reads it, not an
    identifier: an id would need a table mapping it to a title, which is the
    knowledge about modules the form is free of. `toggle` marks the boolean
    that switches its whole section — the UI disables the rest of the group
    when it is off. `advanced` folds the field away until asked for.

    Every hint is optional and a field without any renders as it always did.
    """
    hints: dict[str, Any] = {}
    if group is not None:
        hints["x-group"] = group
    if toggle:
        hints["x-toggle"] = True
    if widget is not None:
        hints["x-widget"] = widget
    if advanced:
        hints["x-advanced"] = True
    return hints
