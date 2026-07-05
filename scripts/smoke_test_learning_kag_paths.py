"""Smoke test for KAG path resolution.

Run from the project root::

    python scripts/smoke_test_learning_kag_paths.py

Expected output ends with ``learning KAG path smoke test passed``.
"""

import sys
from pathlib import Path

_project_root = Path(__file__).resolve().parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from backend.services.learning.kag_paths import (
    candidate_kag_dirs,
    kag_summary,
    resolve_kag_dir,
)


def main() -> None:
    # 1. Candidate paths
    print("候选 KAG 路径:")
    for p in candidate_kag_dirs():
        print(f"  - {p}")

    # 2. Resolved path
    kag_dir = resolve_kag_dir()
    print(f"\n命中 KAG 路径: {kag_dir}")

    # 3. Summary
    summary = kag_summary(kag_dir)
    print("\nKAG 摘要:")
    print(f"  chunks: {summary['chunks']}")
    print(f"  kg_nodes: {summary['kg_nodes']}")
    print(f"  kg_edges: {summary['kg_edges']}")
    print(f"  questions: {summary['questions']}")
    print(f"  retrieval_docs: {summary['retrieval_docs']}")
    print(f"  dense_index_exists: {summary['dense_index_exists']}")
    print(f"  sparse_index_exists: {summary['sparse_index_exists']}")
    print(f"  graph_index_exists: {summary['graph_index_exists']}")

    # 4. Assertions
    assert kag_dir.exists(), f"Resolved KAG directory does not exist: {kag_dir}"
    assert summary["ok"] is True, f"summary['ok'] expected True, got {summary['ok']}"
    assert summary["chunks"] is None or summary["chunks"] > 10_000, (
        f"chunks too low: {summary['chunks']}"
    )
    assert summary["kg_nodes"] is None or summary["kg_nodes"] > 1_000, (
        f"kg_nodes too low: {summary['kg_nodes']}"
    )
    assert summary["retrieval_docs"] is None or summary["retrieval_docs"] > 10_000, (
        f"retrieval_docs too low: {summary['retrieval_docs']}"
    )
    has_at_least_one_index = (
        summary["dense_index_exists"]
        or summary["sparse_index_exists"]
        or summary["graph_index_exists"]
    )
    assert has_at_least_one_index, "No index directories found"

    print("\nlearning KAG path smoke test passed")


if __name__ == "__main__":
    main()
