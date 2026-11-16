# Backend — KTRU card service

FastAPI service with independent assertions, optional local open-model extraction and deterministic catalog checking.
Sessions live in memory in one process. The immutable catalog is SQLite. See the root [README](../README.md).

Search uses FTS5 for a clearly named product and combines FTS5 with local multilingual E5 INT8 embeddings for
uncertain descriptions. The full-catalog family index is built once; runtime inference stays on CPU and offline.
Results and spelling corrections are cached for the immutable snapshot. Semantic proposals ask for family
confirmation and expand every ACTIVE non-template member before checking characteristics.

Use the root [README](../README.md) to build a demo snapshot or import a catalog.
The optional index builder downloads pinned MIT-licensed E5 weights from Hugging Face and publishes a
self-contained index under `.ktru-build/vector/<snapshot_id>/current`. The Compose `hybrid` profile
exposes the builder. The API mounts the index read-only and falls back to FTS5 if the default index is absent.
`KTRU_VECTOR_INDEX_DIR` overrides its location, `KTRU_EMBED_THREADS` controls its thread count, and
`KTRU_SEARCH_MODE=lexical` selects FTS5 directly.

`scripts/benchmark_search.py` measures family recall and query latency with supplied development fixtures.
Provide their locations using `--gold` and `--texts` and pass `--snapshot` to select the catalog.

```bash
uv sync --python 3.12
uv run --python 3.12 uvicorn src.api.ktru.app:app --host 127.0.0.1 --port 8010   # set KTRU_SNAPSHOT_DIR for your snapshot
uv run --python 3.12 pytest -q --cov=src.ktru --cov-fail-under=80
uv run --python 3.12 ruff check src tests scripts/export_contract.py
uv run --python 3.12 python scripts/export_contract.py --check
```

- `src/ktru/contracts.py` — `s0-4` models, stage results and dependency Protocols.
- `src/ktru/catalog/` — immutable reader, provenance and shared catalog-domain validation.
- `src/ktru/engine/` — values, binding, compatibility, fields, questions and readiness; `resolve.py` assembles the card.
- `src/ktru/service/stages.py` and `engine/adapters.py` — extraction, retrieval and deterministic stage adapters.
- `src/ktru/service/session_store.py` — injected one-process store with atomic CAS, replay limits and isolated response copies.
- `src/ktru/service/pipeline.py` — accepted input transitions, injected stages and undo.
- `src/ktru/engine/source_map.py` — original/search code-point maps and checked span fixes.
- `src/ktru/contract_validation.py` — published-card source and reference validation.
- `src/api/ktru/app.py` — HTTP routes under `/api/ktru`.
- `scripts/snapshot.py` — unpack the organizer zip and build the snapshot once (used by the catalog Compose overlay).
- `Dockerfile` — python:3.12-slim + uv, non-root; same image runs the api and the snapshot builder.

`/parse` accepts text and returns a rules preview. `/analyze` calls the configured open model and publishes only on
the same current revision. Configure `KTRU_LLM_URL` and `KTRU_LLM_MODEL` in the process environment; local Ollama
uses `http://127.0.0.1:11434` and an installed open model. Without them model extraction is disabled; rules remain available.

After changing Pydantic contracts, run `uv run --python 3.12 python scripts/export_contract.py` and migrate consumers.
The generated TypeScript file must pass `--check`. Model errors retain rules and return `status=partial`.
Timings separate rule extraction, retrieval, model extraction, resolution and total transition time.
CardState retains available model manifest data. `KTRU_LLM_REASONING_EFFORT` is an optional explicit request setting;
it is unset by default. Development timing probes did not meet 2.5 s and do not establish Linux resource limits.
