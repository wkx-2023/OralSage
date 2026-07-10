"""Smoke test: verify the retrieval layer resolves to the real KAG path.

Run from the project root::

    python scripts/smoke_test_retrieval_kag_path.py

Expected output ends with ``retrieval KAG path smoke test passed``.
"""

import sys
from pathlib import Path

_project_root = Path(__file__).resolve().parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from backend.services.retrieval.chunk_store import load_chunks, reset_cache
from backend.services.retrieval.index_store import (
    bm25_index_path,
    reset_index_caches,
    vector_index_dir,
)
from backend.services.retrieval import search_knowledge_base


def main() -> None:
    reset_cache()
    reset_index_caches()

    # 1. Chunks
    chunks = load_chunks()
    print(f"chunks count: {len(chunks)}")

    # 2. BM25 index
    bm25_path = bm25_index_path()
    print(f"bm25 index path: {bm25_path}")

    # 3. Vector index
    vec_dir = vector_index_dir()
    print(f"vector index dir: {vec_dir}")

    # 4. Search
    topic = "牙周炎"
    results = search_knowledge_base(topic, top_k=5)
    print(f"search results for '{topic}': {len(results)}")

    print("\n前 3 个搜索结果:")
    for i, ch in enumerate(results[:3]):
        d = ch.to_dict()
        print(f"  [{i+1}] id={d['id'][:60]}...")
        print(f"      title={d['title'][:80]}")
        print(f"      source_type={d['source_type']}")
        print(f"      score={d['score']}")

    # Assertions
    assert len(chunks) > 10_000, f"chunks count too low: {len(chunks)}"
    assert bm25_path.exists(), f"bm25 index not found: {bm25_path}"
    assert vec_dir.exists(), f"vector index dir not found: {vec_dir}"
    assert len(results) > 0, f"expected >0 results for '{topic}', got {len(results)}"

    print("\nretrieval KAG path smoke test passed")


if __name__ == "__main__":
    main()
