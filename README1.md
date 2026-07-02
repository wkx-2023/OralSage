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
- Image recognition uses `VISION_LLM_*`, falling back to `QWEN_API_KEY`/`QWEN_BASE_URL`.
- Records, ASR correction, record extraction, record generation, evidence search, and literature term extraction stay rule/retrieval based for now.

If no LLM key is configured, the literature agent still searches real PubMed
papers and returns a conservative rule-based report.

## Cloud Vision Image Assistant

The image recognition assistant is implemented without Nexent through:

- `POST /api/image/analyze`
- `POST /api/image/analyze-upload`
- `POST /api/agents/oral_disease_image_recognition_assistant/invoke`

For the first cloud-backed version, configure a Qwen-VL OpenAI-compatible
endpoint:

```powershell
VISION_LLM_API_KEY=your_dashscope_key
VISION_LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
VISION_LLM_MODEL=qwen2.5-vl-7b-instruct
VISION_LLM_TIMEOUT_SECONDS=180
VISION_MAX_IMAGE_MB=10
```

The cloud model is used to return a structured oral imaging report plus
normalized `bbox_2d` coordinates. The frontend image page draws those boxes
over the uploaded image when coordinates are returned. This cloud model can be
replaced later by a local OralPath service by changing `VISION_LLM_BASE_URL`
and `VISION_LLM_MODEL`.
