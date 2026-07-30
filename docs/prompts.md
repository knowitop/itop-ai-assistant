# Customizing prompts

All LLM prompts are plain Markdown templates. The packaged defaults ship inside the Python package, in [`assistant/src/itop_ai_assistant/prompts/intake/`](../assistant/src/itop_ai_assistant/prompts/intake). You can override any of them without touching the code.

---

## Editing via the admin UI

The quickest way: open **Admin UI → Prompts**, click a prompt name in the sidebar, edit the text, and click **Save**. The change takes effect from the next processed ticket — no restart needed. Overridden prompts are flagged with a badge. Any prompt can be reset to its packaged default with **Reset to default**.

---

## Editing via files

For version-controlled overrides or deployment automation, use the `PROMPTS_DIR` environment variable:

1. Set `PROMPTS_DIR` to a directory on the host, e.g. `/etc/itop-ai/prompts`
2. Place override files under `<PROMPTS_DIR>/intake/` with the same names as the defaults:

```
/etc/itop-ai/prompts/
└── intake/
    └── system.md   # overrides only this one prompt
```

Files you place here shadow the packaged defaults. Files you do not place keep their defaults. Prompt files are re-read on every processing run — no restart needed after editing.

---

## Prompt files

The intake module runs as one agent session, so its prompts are not per-LLM-call pairs but the three messages that open the session:

| File | Role in the session | Sent when |
|------|--------------------|-----------|
| `system.md` | The system message: who the agent is, the rules it works under, when to ask versus when to hand off | Always |
| `catalog_human.md` | The service catalog available to the requester's organization | Only for an unclassified ticket — a ticket that already has a service and a subcategory cannot be reclassified, so the list is omitted |
| `ticket_human.md` | This ticket: title, description, current classification, conversation so far | Always |

Everything after these three messages is the agent's own doing — which tool it calls, and what it writes. Note that **tool descriptions are code, not prompts**: they live in the docstrings in `assistant/src/agents/intake/tools.py` and are not overridable through the admin UI.

---

## Placeholders

Prompts use `{placeholder}` variables substituted at runtime. Each template has its own allowed set — a placeholder valid in one file is rejected in another.

| Placeholder | Available in | Value |
|-------------|-------------|-------|
| `{services}` | `catalog_human` | Formatted list of Services available to the requester's organization |
| `{caller_name}` | `ticket_human` | Display name of the ticket creator |
| `{title}` | `ticket_human` | Ticket title |
| `{description}` | `ticket_human` | Ticket description (HTML converted to Markdown) |
| `{service_context}` | `ticket_human` | Current Service and ServiceSubcategory, including the subcategory description used as completeness criteria |
| `{conversation}` | `ticket_human` | Public log history rendered as an XML block, with a `role` per entry (`requester` / `self` / `agent`) |

`system.md` takes no placeholders.

Placeholder names are validated on save. If a template references an unknown name, the save is rejected with an error showing which placeholder is unrecognized. The same validation runs at startup, so a broken override file fails the boot rather than a live ticket.

> [!TIP]
> After editing a prompt, run the real-LLM test suite — it is the only thing that checks the prompts against an actual model: `cd assistant && uv run pytest test/integration` (needs `.env.test`, see `.env.test.dist`).
