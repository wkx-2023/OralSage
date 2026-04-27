# Data Pipeline Scripts

This directory contains the rebuild pipeline for creating the local,
Nexent-free knowledge base used by the decision assistant.

Implemented:

- `build_knowledge_base.py`: parse raw PDFs, MSD JSONL, and clinical case Excel files into local retrieval JSONL chunks.

Quick first build:

```powershell
cd D:\craic
python scripts\data_pipeline\build_knowledge_base.py --skip-textbooks
```

Full build, including the 14 textbooks:

```powershell
cd D:\craic
python scripts\data_pipeline\build_knowledge_base.py
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
```

OCR note:

The current textbook PDFs already contain an OCR text layer, but Chinese OCR quality is uneven.
`build_knowledge_base.py` applies a small conservative cleanup pass, stores `metadata.ocr_quality`
for every PDF chunk, and the local retriever down-weights low-quality OCR chunks. This makes the
textbook knowledge usable for retrieval, but it does not fully correct OCR mistakes. For clinical
grade textbook recall, rebuild the textbook text layer with a dedicated Chinese OCR engine such as
PaddleOCR/CnOCR, then run this script again.

Planned follow-up scripts:

- `ingest_documents.py`: parse PDFs, OCR text where needed, and normalize documents.
- `build_clinical_cases.py`: convert deidentified outpatient records into SOAP-style JSONL.
- `chunk_knowledge_base.py`: build retrieval chunks from normalized text.
- `build_indexes.py`: build local vector and sparse retrieval indexes.
- `validate_data_layout.py`: validate required files and report dataset counts.

Training and evaluation scripts copied from the source bundle are under `../model_training/`.
