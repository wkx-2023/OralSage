# OralPath Rebuild Shell

This repository currently keeps only:

- `frontend/` static UI design assets
- `backend/` FastAPI route interfaces and request schemas

All previous business logic, data, agents, generated caches, and helper scripts have been removed so the project can be rebuilt from a clean interface shell.

## Run API Shell

```powershell
cd D:\craic
python -m uvicorn backend.main:app --reload --port 8086
```

Open `http://localhost:8086`.

## Frontier Literature Agent

The local rebuild now implements the literature agent without Nexent:

- `POST /api/literature/terms`
- `POST /api/literature/search`
- `POST /api/agents/oral_medicine_search_assistant/invoke`

Configuration is optional for PubMed and required for Elsevier fallback. Keep
real secrets in the ignored project-root file `D:\craic\.env`; use
`backend/.env.example` only as a template:

- `PUBMED_CONTACT_EMAIL`
- `ELSEVIER_API_KEY`
- `ELSEVIER_INSTITUTION_TOKEN`
- `LITERATURE_LLM_API_KEY`
- `LITERATURE_LLM_BASE_URL`
- `LITERATURE_LLM_MODEL`

LLM assignment in the local rebuild:

- Decision assistant uses `DECISION_LLM_*`, falling back to `QWEN_*`.
- Frontier literature report generation uses `LITERATURE_LLM_*`, falling back to `QWEN_*`.
- Records, ASR correction, record extraction, record generation, evidence search, and literature term extraction stay rule/retrieval based for now.

If no LLM key is configured, the literature agent still searches real PubMed
papers and returns a conservative rule-based report.
