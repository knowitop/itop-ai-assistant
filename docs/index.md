# iTop AI Assistant

Python middleware that adds an AI layer on top of the
[Combodo iTop](https://www.itophub.io/) ITSM platform. iTop stays the system of
record; this service adds intelligence between users and engineers.

Implemented today is the **intake** module: an iTop webhook on a new ticket →
classification against the service catalog → at most one clarifying question at
a time in the public log → an internal note handing the ticket to an engineer.
All AI actions run under a dedicated iTop service account, so every comment is
attributed and auditable.

## How it works

The agent decides the order; the tools enforce the rules. The round limits, the "one question per run" rule and the "stop once an engineer takes the ticket" guard are plain code, not instructions in a prompt — a model that misbehaves gets its call rejected rather than the ticket damaged. Each action — classify, ask, hand off, quote similar tickets — has its own switch, and a switched-off action's tool is never handed to the model at all ([which actions the module performs](configuration.md#which-actions-the-module-performs)).

### Examples

**Scenario 1 — incomplete ticket**

A user opens a ticket in the service portal:
> **Title:** printer broken  
> **Description:** Not printing.

The service subcategory is *Hardware*, which requires: device model, OS and exact error. The description provides none of this. The assistant posts in the public log within seconds:

> **AI Assistant**
>
> Thank you for reaching out! To help us resolve this quickly, could you please provide:  
> — the manufacturer and model of the printer (e.g. HP LaserJet 400 M401dn);  
> — your operating system and version;  
> — the exact error message or what happens when you try to print.

**Scenario 2 — complete ticket**

Another user submits:
> **Title:** HP LaserJet 400 M401dn not printing after Windows 11 update  
> **Description:** My HP LaserJet 400 M401dn stopped printing after a Windows 11 update yesterday evening. Error: "Driver unavailable". Already restarted both printer and PC.

All required fields are present. No question is asked. Instead, the engineer immediately sees an internal note:

> **AI Assistant** (internal note)
>
> **Issue:** HP LaserJet 400 M401dn stopped printing after a Windows 11 update. Error: "Driver unavailable".  
> **Already tried:** Restarted printer and PC.  
> **Suggested next step:** Reinstall or update the printer driver from HP's website; check if Windows Update pushed an incompatible driver version.

### The flow

```
Ticket created           User commented
       │                        │
       └───────────┬────────────┘
                   │
                   ▼
┌─ guard (plain code) ──────────────────────────────┐
│  Already processed?      ──yes──▶  stop           │
│  Engineer assigned?      ──yes──▶  stop           │
│  Last comment was ours?  ──yes──▶  stop           │
└─────────────────────────┬─────────────────────────┘
                          │ no
                          ▼
┌─ agent session ───────────────────────────────────┐
│  In the prompt: the ticket, the conversation,     │
│  plus the service catalog while the ticket is     │
│  still unclassified.                              │
│                                                   │
│  The agent picks a tool, the tool enforces:       │
│                                                   │
│   classify    service + subcategory checked       │
│               against the catalog, then the       │
│               session continues                   │
│   ask         one question in the public log,     │
│               session ends                        │
│   hand off    internal note for the engineer,     │
│               session ends                        │
└─────────────────────────┬─────────────────────────┘
                          ▼
Ticket marked processed, or left waiting for a reply
that arrives as the next webhook
```

## Documentation

| Page | What it covers |
|---|---|
| [Setup](setup.md) | Setup wizard walkthrough, manual iTop configuration, and trying the assistant on your own data first |
| [Admin UI](admin-ui.md) | Connections, Modules, Prompts, Runs and Vector index screens |
| [Configuration](configuration.md) | Environment variables, module settings, dry run, LLM tracing, supported LLM providers and the vector index |
| [Customizing prompts](prompts.md) | Editing the LLM prompts via the UI or from files |
| [Telemetry](telemetry.md) | The anonymous daily document — every field, who receives it, and how to switch it off |

## Project links

- Source: [github.com/knowitop/itop-ai-assistant](https://github.com/knowitop/itop-ai-assistant)
- Package: [pypi.org/project/itop-ai-assistant](https://pypi.org/project/itop-ai-assistant/)
- Image: [hub.docker.com/r/knowitop/itop-ai-assistant](https://hub.docker.com/r/knowitop/itop-ai-assistant)
