# iTop AI Assistant

Python middleware that adds an AI layer on top of the
[Combodo iTop](https://www.itophub.io/) ITSM platform. iTop stays the system of
record; this service adds intelligence between users and engineers.

Implemented today is the **intake** module: an iTop webhook on a new ticket →
classification against the service catalog → at most one clarifying question at
a time in the public log → an internal note handing the ticket to an engineer.
All AI actions run under a dedicated iTop service account, so every comment is
attributed and auditable.

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
