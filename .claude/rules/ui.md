---
paths:
  - "ui/**"
---

# Admin SPA (`ui/`)

Setup wizard, settings, prompts, run monitoring. Vite + React + TypeScript +
Mantine. Maintained primarily with AI assistance by a non-frontend developer, so
**simplicity beats elegance**. These constraints are mandatory — the default
instinct to reach for a library is exactly what they forbid:

- **Minimal dependencies**: `react`, `react-dom`, `react-router-dom`,
  `@mantine/core`, `@mantine/form` and their peers — nothing else. No Redux, no
  TanStack Query, no axios, no CSS-in-JS. State is `useState`; HTTP is the single
  fetch wrapper in `api.ts`.
- **Flat structure**: one file per screen (`SetupWizard.tsx`, `Connections.tsx`,
  `Modules.tsx`, `Prompts.tsx`, `Runs.tsx`, `Vector.tsx`) plus `api.ts` and
  `Layout.tsx`. No hook factories, barrel files or clever abstractions.
- **Pin exact versions** in `package.json` (no `^`/`~`), commit the lock file;
  upgrade only when something requires it.
- The prompt editor is a plain Mantine `Textarea`. CodeMirror only if syntax
  highlighting becomes a real need.
- The connection form is generated from `GET /api/setup/llm-providers` — do not
  duplicate the provider list in TypeScript.
- The module config form is generated from `GET /api/config/{module}/schema` —
  **never** add a list of a module's fields, labels or sections to TypeScript.
  Anything the form needs about a field travels in the schema itself
  (`settings/ui_hints.py`, ADR-025): a new field or a new module must render
  without touching `ui/`. Labels, descriptions and group headings arrive
  **already translated** — the module ships `locales/<lang>.json` and the
  backend applies it (ADR-030), so the request carries `?lang=${i18n.language}`
  and the screen refetches when the language changes. A module's field is never
  translated in `ui/src/locales/*.json` — nothing reads such a key.
- Builds into `assistant/src/itop_ai_assistant/ui_dist` — the SPA is part of the
  Python package (ADR-032), which is what lets a `pip install` serve the setup
  wizard. Served by FastAPI at `/ui` (API stays under `/api`). In dev use the
  vite proxy to `:8001` — no CORS. The admin token lives in `localStorage`;
  401 shows the token entry screen.
- **New user-facing strings: add only to `locales/en.json` by default.** The
  other locales (`cs`, `de`, `es`, `fr`, `it`, `kk`, `pl`, `ru`, `sk`, `uk`,
  `zh`) are translated on request, not automatically on every UI change — ask
  before touching them. This is about the SPA's own strings; a module's field
  texts are not here at all (see the module config form above).
- **Locale bundles are loaded through `import.meta.glob`, never a template
  `import()`.** A variable dynamic import compiles to a runtime helper with a
  fixed list of files and throws on anything else — which is how a stale
  `ru-RU` in `localStorage` took the whole built app down. `i18n.ts` normalizes
  the tag, falls back to `en`, and loads the English bundle alongside the chosen
  one (`fallbackLng` only reaches bundles that are already loaded).

```bash
npm ci          # install pinned dependencies
npm run dev     # vite dev server, proxies /api, /health and /version to :8001
npm run build   # tsc --noEmit + production build into the Python package
```
