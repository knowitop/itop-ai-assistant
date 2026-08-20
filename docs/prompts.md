# Customizing prompts

All LLM prompts are plain Markdown templates. The packaged defaults ship inside the Python package, in [`assistant/src/itop_ai_assistant/agents/intake/prompts/`](../assistant/src/itop_ai_assistant/agents/intake/prompts). You can override any of them without touching the code.

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

> [!IMPORTANT]
> If you overrode `system.md` in an earlier version, re-read it after upgrading. The system message is now assembled from five files, and `system.md` is only the base — the instructions for classification, clarification, the handoff note and similar tickets moved into the four `system_*.md` files next to it. An older override keeps working, but it still contains those sections, so the model receives them twice: once from your copy and once from the packaged fragment. Delete from your override everything below "How to work", or override the individual fragments instead.

Files you place here shadow the packaged defaults. Files you do not place keep their defaults. Prompt files are re-read on every processing run — no restart needed after editing.

---

## Prompt files

The intake module runs as one agent session, so its prompts are not per-LLM-call pairs but the three messages that open the session. The first of them is assembled from five files — a base plus one fragment per [action you switched on](configuration.md#intake-module-settings), so that a switched-off action does not leave its instructions in the model's context:

| File | Role in the session | Sent when |
|------|--------------------|-----------|
| `system.md` | The base of the system message: who the agent is and the rules it works under | Always |
| `system_classify.md` | How to pick a service and a subcategory | `intake.classify_enabled` |
| `system_clarify.md` | When to ask the requester, and how to write to them | `intake.clarify_enabled` |
| `system_handoff_note.md` | How to write the note for the engineer | `intake.handoff_note_enabled` |
| `system_similar.md` | How to quote similar solved tickets in that note | `intake.similar_enabled`, and only where vector search is configured |
| `catalog_human.md` | The service catalog available to the requester's organization | Only for an unclassified ticket — a ticket that already has a service and a subcategory cannot be reclassified, so the list is omitted |
| `ticket_human.md` | This ticket: title, description, current classification, conversation so far | Always |

The fragments are joined in the order of the table, separated by a blank line. Which of them a run received is not guesswork: the run's journal in the admin UI opens with the composition of actions it ran under.

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

The five `system*.md` files take no placeholders.

Placeholder names are validated on save. If a template references an unknown name, the save is rejected with an error showing which placeholder is unrecognized. The same validation runs at startup, but what it does then depends on whose template is broken — see [When an upgrade breaks an override](#when-an-upgrade-breaks-an-override).

---

## When an upgrade breaks an override

A placeholder that is valid today can be dropped in a later version. Your override keeps the old name — it is still called `system.md`, so nothing about it looks stale — and after the upgrade it refers to a placeholder that no longer exists. Validation on save cannot prevent this: it checked your text against the placeholders that existed when you saved it.

**This does not stop the service.** At startup the assistant tells its own templates from yours:

- a broken template of **yours** — a warning in the log, and the prompt marked **broken** in **Admin UI → Prompts**, with the exact error;
- a broken template of **ours** — the service refuses to start. That is a defect in the distribution, not something you can fix from the UI.

Your text is never replaced behind your back. The broken override stays in effect, which means the module using it fails on every run until the text is fixed. The trade is deliberate: a module you can see is broken, in an admin UI that is up, beats a prompt you wrote being quietly swapped for ours.

To fix it, open **Admin UI → Prompts**, pick the prompt marked *broken*, read the error, and either correct the text or press **Reset to default**. The next processed ticket uses the corrected prompt — no restart.

A file whose name matches no built-in prompt is treated the same way: `sytem.md` next to `system.md`, or a leftover from an older version, is not read, and it is listed as *not applied* above the prompt list instead of failing the boot.

> [!TIP]
> After editing a prompt, run the real-LLM test suite — it is the only thing that checks the prompts against an actual model: `cd assistant && uv run pytest test/integration` (needs `.env.test`, see `.env.test.dist`).
