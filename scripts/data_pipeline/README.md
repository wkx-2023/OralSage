# Data Pipeline Scripts

This directory contains the rebuild pipeline for creating the local,
Nexent-free knowledge base used by the decision assistant.

Implemented:

- `build_knowledge_base.py`: parse raw PDFs, MSD JSONL, and clinical case Excel files into local retrieval JSONL chunks.
- `build_indexes.py`: build local BM25 and vector indexes from the JSONL chunks.
- `evaluate_retrieval.py`: run a small retrieval smoke evaluation against seeded pathology questions.

Quick first build:

```powershell
cd D:\craic
python scripts\data_pipeline\build_knowledge_base.py --skip-textbooks
```

Full build, including the 14 textbooks:

```powershell
cd D:\craic
python scripts\data_pipeline\build_knowledge_base.py
python scripts\data_pipeline\build_indexes.py
python scripts\data_pipeline\evaluate_retrieval.py --top-k 8
```

Only rebuild the 14 textbook PDFs:

```powershell
cd D:\craic
python scripts\data_pipeline\build_knowledge_base.py --only-textbooks --textbook-source auto
```

Re-OCR the textbook PDFs with PaddleOCR before rebuilding:

```powershell
cd D:\craic
conda run -n rag_test2 python scripts\data_pipeline\ocr_textbooks_paddle.py
python scripts\data_pipeline\build_knowledge_base.py --only-textbooks --textbook-source ocr
python scripts\data_pipeline\build_indexes.py
```

OCR note:

The current textbook PDFs already contain an OCR text layer, but Chinese OCR quality is uneven.
`build_knowledge_base.py` applies a small conservative cleanup pass, stores `metadata.ocr_quality`
for every PDF chunk, and the local retriever down-weights low-quality OCR chunks. This makes the
textbook knowledge usable for retrieval, but it does not fully correct OCR mistakes. For clinical
grade textbook recall, rebuild the textbook text layer with a dedicated Chinese OCR engine such as
PaddleOCR/CnOCR, then run this script again.

Current retrieval artifacts:

- `data/processed/knowledge_base/*.jsonl`: enriched chunks with source, page, chapter, section, content type, entities, quality score, parent id, and original metadata.
- `data/indexes/bm25/index.json.gz`: local sparse BM25 index.
- `data/indexes/vector/vectors.npy`: local vector index. It uses `sentence-transformers` when a configured local embedding model is present; otherwise it falls back to deterministic hashing vectors so the pipeline remains runnable.
- `data/processed/retrieval_eval_report.json`: latest retrieval smoke-evaluation report.

Resumable bge-m3 vector build:

```powershell
cd D:\craic
$env:HF_HOME="D:\craic\models\hf_cache"
$env:HUGGINGFACE_HUB_CACHE="D:\craic\models\hf_cache\hub"
$env:TRANSFORMERS_CACHE="D:\craic\models\hf_cache\transformers"
$env:SENTENCE_TRANSFORMERS_HOME="D:\craic\models\hf_cache\sentence_transformers"

python scripts\data_pipeline\build_indexes.py --skip-bm25 --batch-size 4
```

Vector builds write batch progress to `data/indexes/vector/build_progress/`.
If the command is interrupted, run the same command again and it will resume
from the last completed batch. Use `--force-vector` only when you intentionally
want to discard progress and rebuild from the first chunk.

Planned follow-up scripts:

- `ingest_documents.py`: parse PDFs, OCR text where needed, and normalize documents.
- `build_clinical_cases.py`: convert deidentified outpatient records into SOAP-style JSONL.
- `validate_data_layout.py`: validate required files and report dataset counts.

Training and evaluation scripts copied from the source bundle are under `../model_training/`.
