# iTop AI Assistant

AI-powered middleware for [Combodo iTop](https://www.itophub.io/) that reduces ticket back-and-forth and helps engineers start working faster.

---

## The problem

Engineers waste time on tickets that arrive without enough information: vague descriptions, missing hardware details, no steps to reproduce. Before any work can start, they have to write back to the user and wait. This creates delays, drops SLA metrics and frustrates everyone.

---

## How it works

> **The engineer sees the ticket only when it's ready to work on.**

When a new ticket arrives, the assistant intercepts it via webhook — no changes to iTop itself are needed. It works in two stages:

1. **Classify** — if the ticket has no service or subcategory set, the assistant queries iTop for the available options and picks the best match based on the title and description. If it cannot determine the right category confidently, it posts one clarifying question in the public log and waits for the user to reply.

2. **Evaluate** — once the category is known, the assistant uses the subcategory's description as the completeness criteria. This means the questions it asks are specific to the service context, not generic prompts.

- If the description is **complete** — it generates a structured internal note for the engineer and marks the ticket ready.
- If the description is **incomplete** — it posts one focused clarifying question in the iTop public log. The user replies through the portal as usual, the assistant re-evaluates and either asks one follow-up or proceeds to enrich. Maximum two rounds per stage.

All AI actions are performed under a dedicated iTop service account, so every comment is clearly attributed and auditable.

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
Ticket created          User commented
        │                       │
        └──────────┬────────────┘
                   │
                   ▼
         Already processed?  ──yes──▶  stop
         Engineer assigned?  ──yes──▶  stop
                   │ no
                   ▼
        Service/subcategory set?
                   │
        ┌──────────┴──────────────────────┐
        │ yes                             │ no
        │                                 ▼
        │                    LLM picks category from iTop
        │                                 │
        │                    ┌────────────┴──────────┐
        │                    │ confident             │ unsure
        │                    ▼                       ▼
        │             category set        Ask clarifying question
        │                    │            in public log,
        │                    │            wait for reply
        │                    │            (triggers new webhook)
        └──────────┬─────────┘
                   ▼
        Is description sufficient?
                   │
        ┌──────────┴──────────────┐
        │ yes                     │ no
        ▼                         ▼
Post internal note          Ask clarifying question
for engineer                in public log,
        │                   wait for user reply
        │                   (triggers new webhook)
        │
        ▼
Mark ticket processed
```

---

## Requirements

- **iTop 3.x** with REST API enabled
- **Redis** (included in the Docker Compose stack)
- **OpenAI-compatible LLM endpoint** — local (LM Studio, Ollama) or cloud (OpenAI, Azure, LiteLLM Proxy)
- **Docker and Docker Compose** for the quick start; [uv](https://docs.astral.sh/uv/) for local development

---

## Quick start

```bash
git clone https://github.com/knowitop/itop-ai-assistant.git
cd itop-ai-assistant/docker
cp .env.dist .env
docker compose up -d
```

The compose stack starts iTop, Redis and the assistant together. If you already have an iTop or Redis instance, comment out those services in `docker-compose.yml`.

Once running:

| Service   | URL                          |
|-----------|------------------------------|
| iTop      | `http://localhost:8000`      |
| Admin UI  | `http://localhost:8001/ui`   |
| API docs  | `http://localhost:8001/docs` |

Open `http://localhost:8001/ui` — the **Setup Wizard** starts automatically and walks you through all the required steps.

---

## Documentation

- [**Setup**](docs/setup.md) — setup wizard walkthrough and manual iTop configuration
- [**Admin UI**](docs/admin-ui.md) — Connections, Modules, Prompts and Runs screens
- [**Configuration**](docs/configuration.md) — environment variables, module settings and supported LLM providers
- [**Customizing prompts**](docs/prompts.md) — editing LLM prompts via UI or files

---

## Roadmap

The current release covers the first-contact enrichment loop — intercepting new tickets, asking clarifying questions and preparing them for the engineer. Planned next phases:

- **Pattern analysis** — background jobs that surface recurring issues and trends across tickets.
- **Knowledge base maintenance** — automatically flag outdated KB articles and suggest updates based on resolved tickets.
- **Change Management review** — AI-assisted risk and impact assessment for RFCs.
- **Engineer widget** — contextual AI sidebar inside the iTop UI showing similar past tickets and suggested actions.
- **User memory** — persistent context per user across tickets: no repeated questions about device or department, automatic adaptation to technical vs. non-technical communication style and pattern detection across a user's ticket history.

Feedback and ideas are welcome in [GitHub Issues](../../issues).

---

## Local development

Requires [uv](https://docs.astral.sh/uv/).

```bash
cd assistant
uv sync
cp docker/.env.dist .env   # fill in LLM and iTop settings
uvicorn src.main:app --host 0.0.0.0 --port 8001 --reload
```

**Tests:**

```bash
cd assistant
uv run pytest              # unit tests (mocked LLM, iTop and Redis)
uv run pytest --cov=src    # with coverage report
```

**Admin UI** (requires Node.js; the dev server proxies `/api` to the backend on `:8001`):

```bash
cd ui
npm ci
npm run dev     # hot-reload dev server
npm run build   # production build into ui/dist
```

Architecture details and development conventions are in [CLAUDE.md](CLAUDE.md).

---

## License

[AGPL-3.0](LICENSE)
