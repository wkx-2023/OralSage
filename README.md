# Oral Edu KAG BGE-M3 Knowledge Base

Build date: 2026-07-01

This package contains the KAG-ready oral medicine knowledge base and retrieval indexes.

## Contents

```text
data/kb/kag_bge_m3/
  chunks.jsonl
  kg_nodes.jsonl
  kg_edges.jsonl
  concept_edges.jsonl
  kg_chunk_links.jsonl
  questions.jsonl
  question_links.jsonl
  retrieval_corpus.jsonl
  source_layers.jsonl
  manifest.json
  validation_report.json
  quality_repair_report.json
  indexes/
    graph/
    sparse/
    dense_bge_m3/

scripts/data_pipeline/
  query_kag_bge_m3.py
  validate_kag_bge_m3_kb.py
  repair_kag_bge_m3_quality.py
  rebuild_kag_bge_m3_sparse.py
  sync_kag_dense_metadata.py
  build_kag_bge_m3_vectors.py
```

## Current Quality Status

```text
validation ok: true
chunks: 21,737
kg_nodes: 9,907
kg_edges: 5,758
concept_edges: 4,927
kg_chunk_links: 9,434
questions: 3,161
question_links: 6,287
retrieval_docs: 34,805
```

Core-node evidence coverage:

```text
core_nodes_with_any_chunk_path: 1,390 / 1,390
core_nodes_without_any_chunk_path: 0
```

Indexes:

```text
graph index: ready
sparse BM25 index: ready
dense BGE-M3 FAISS index: ready
dense vectors: 34,805 x 1,024
```

Quality metadata has been added for:

```text
exact_duplicate_primary
exact_duplicate_suppressed
reference_heavy
boilerplate_disclaimer
```

These flags are used by the query script for retrieval re-ranking and quality penalties.

## Required Model

The BGE-M3 model files are not included in this package.

Expected local model path by default:

```text
models/embeddings/bge-m3
```

You can download it with:

```bash
huggingface-cli download BAAI/bge-m3 --local-dir models/embeddings/bge-m3
```

The dense index was built with:

```text
model: BAAI/bge-m3
provider: sentence_transformers
pooling: CLS pooling + normalize
dims: 1024
max_length: 512
```

The query script uses ONNX Runtime with the same CLS pooling configuration.

## Recommended Python Environment

```bash
pip install jieba numpy onnxruntime transformers faiss-cpu
```

Optional, only needed for rebuilding vectors:

```bash
pip install sentence-transformers FlagEmbedding
```

## Validate

From the package root:

```bash
python scripts/data_pipeline/validate_kag_bge_m3_kb.py --kag-dir data/kb/kag_bge_m3
```

Expected:

```text
ok: true
errors: []
warnings: []
```

## Query Examples

```bash
python scripts/data_pipeline/query_kag_bge_m3.py --kag-dir data/kb/kag_bge_m3 --model-dir models/embeddings/bge-m3 --query "儿童龋病" --top-k 5

python scripts/data_pipeline/query_kag_bge_m3.py --kag-dir data/kb/kag_bge_m3 --model-dir models/embeddings/bge-m3 --query "畸形舌侧尖" --top-k 5

python scripts/data_pipeline/query_kag_bge_m3.py --kag-dir data/kb/kag_bge_m3 --model-dir models/embeddings/bge-m3 --query "种植体周病管理" --top-k 5
```

Retrieval combines:

```text
alias seed
sparse BM25
graph local expansion
BGE-M3 dense FAISS
quality-aware re-ranking
```

## Main Retrieval Chain

```text
query
-> alias / dense / sparse candidate retrieval
-> KG local expansion
-> kg_chunk_links
-> evidence chunks
-> source_span_ids / page / document
```

For factual answers, use chunks and spans as final evidence. KG nodes and edges provide structure and explanation paths.

## Notes

- Original source files are not included.
- The package contains derived KB artifacts only.
- Dense `build_progress` files are intentionally excluded.
- `vectors.npy` and `faiss.index` are both included.
- If `retrieval_corpus.jsonl` changes, dense vectors must be rebuilt unless only metadata changes and doc order is preserved.
