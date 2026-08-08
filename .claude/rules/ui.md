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
- Builds into `ui/dist`, served by FastAPI at `/ui` (API stays under `/api`). In
  dev use the vite proxy to `:8001` — no CORS. The admin token lives in
  `localStorage`; 401 shows the token entry screen.
- **New user-facing strings: add only to `locales/en.json` by default.** The
  other locales (`cs`, `de`, `es`, `fr`, `it`, `kk`, `pl`, `ru`, `sk`, `uk`,
  `zh`) are translated on request, not automatically on every UI change — ask
  before touching them.

```bash
npm ci          # install pinned dependencies
npm run dev     # vite dev server, proxies /api, /health and /version to :8001
npm run build   # tsc --noEmit + production build into ui/dist
```
