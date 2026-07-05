# Customizing prompts

All LLM prompts are plain Markdown templates. The packaged defaults live in [`assistant/prompts/enrichment/`](../assistant/prompts/enrichment). You can override any of them without touching the code.

---

## Editing via the admin UI

The quickest way: open **Admin UI → Prompts**, click a prompt name in the sidebar, edit the text, and click **Save**. The change takes effect from the next processed ticket — no restart needed. Overridden prompts are flagged with a badge. Any prompt can be reset to its packaged default with **Reset to default**.

---

## Editing via files

For version-controlled overrides or deployment automation, use the `PROMPTS_DIR` environment variable:

1. Set `PROMPTS_DIR` to a directory on the host, e.g. `/etc/itop-ai/prompts`
2. Place override files under `<PROMPTS_DIR>/enrichment/` with the same names as the defaults:

```
/etc/itop-ai/prompts/
└── enrichment/
    └── evaluate_system.md   # overrides only this one prompt
```

Files you place here shadow the packaged defaults. Files you do not place keep their defaults. Prompt files are re-read on every processing run — no restart needed after editing.

---

## Prompt files

Each LLM call uses a pair of files — a system message and a human message:

| Pair | Files | Purpose |
|------|-------|---------|
| `classify_service` | `classify_service_system.md` / `classify_service_human.md` | Pick the best matching Service from the iTop catalog |
| `classify_subcategory` | `classify_subcategory_system.md` / `classify_subcategory_human.md` | Pick the best matching ServiceSubcategory |
| `classify_ask` | `classify_ask_system.md` / `classify_ask_human.md` | Generate a clarifying question when the category cannot be determined confidently |
| `evaluate` | `evaluate_system.md` / `evaluate_human.md` | Decide whether the ticket description is sufficient; if not, generate the clarifying question text |
| `enrich` | `enrich_system.md` / `enrich_human.md` | Generate a structured internal note for the engineer |

---

## Placeholders

Prompts use `{placeholder}` variables substituted at runtime.

| Placeholder | Available in | Value |
|-------------|-------------|-------|
| `{title}` | all prompts | Ticket title |
| `{description}` | all prompts | Ticket description (HTML stripped to plain text) |
| `{caller_name}` | all prompts | Display name of the ticket creator |
| `{service_context}` | `evaluate` | Service and subcategory details, including the subcategory description used as completeness criteria |
| `{services}` | `classify_service` | Formatted list of Services available in the iTop catalog |
| `{subcategories}` | `classify_subcategory` | Formatted list of ServiceSubcategories for the selected Service |

> [!NOTE]
> The `evaluate` prompt also receives the conversation history (previous exchanges between the user and the assistant), but it is injected as a sequence of chat messages, not a text placeholder — you cannot reference it as `{conversation}` in the template.

Placeholder names are validated on save. If a template references an unknown name, the save is rejected with an error showing which placeholder is unrecognized.
