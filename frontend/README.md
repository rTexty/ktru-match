# Frontend — подбор КТРУ

React 19 + Vite + TanStack Router/Query + Tailwind 4. Single page (`src/app/routes/index.tsx` → `src/components/ktru/KtruPage.tsx`).

- `src/api/ktru.ts` — HTTP client for `/api/ktru/*`.
- `src/api/ktru.generated.ts` — types generated from the backend Pydantic JSON Schema. From `backend/`,
  run `uv run --python 3.12 python scripts/export_contract.py`; use `--check` to detect drift.
- `src/components/ktru/` — page, result card, source-text highlighting, Portal examples panel.

```bash
pnpm install
pnpm dev     # http://localhost:5173, proxies /api/ → http://127.0.0.1:8010 (override with VITE_DEV_PROXY_TARGET)
pnpm build
pnpm lint
```

The UI first calls `/parse`. For a provisional card it calls `/analyze`, unless health reports the model disabled.
It accepts analysis only for the current session and revision. Family selection, answers, edits and undo
recompute the card; unsaved field and numeric drafts are retained. A final code and a ready card are separate states.

```bash
pnpm exec playwright install chromium   # once
pnpm test:e2e
```

The 45 E2E checks recorded during cleanup use mocked API responses. They do not establish real LLM quality.
