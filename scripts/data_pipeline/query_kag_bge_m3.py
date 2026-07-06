from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

try:
    from .kag_solver import (
        DEFAULT_EMBEDDING_MODEL_DIR,
        DEFAULT_KAG_DIR,
        DEFAULT_RERANKER_MODEL_DIR,
        KAGSolver,
        SolverConfig,
        parse_csv_set,
    )
except ImportError:
    from kag_solver import (
        DEFAULT_EMBEDDING_MODEL_DIR,
        DEFAULT_KAG_DIR,
        DEFAULT_RERANKER_MODEL_DIR,
        KAGSolver,
        SolverConfig,
        parse_csv_set,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Query OralSage KAG with BM25 + BGE-M3 + Alias + Graph-PPR + reranker."
    )
    parser.add_argument("--kag-dir", type=Path, default=DEFAULT_KAG_DIR)
    parser.add_argument("--embedding-model-dir", type=Path, default=DEFAULT_EMBEDDING_MODEL_DIR)
    parser.add_argument("--reranker-model-dir", type=Path, default=DEFAULT_RERANKER_MODEL_DIR)
    parser.add_argument("--query", required=True)
    parser.add_argument("--task-type", default="concept_qa")
    parser.add_argument("--top-k", type=int, default=8)
    parser.add_argument("--dense-k", type=int, default=40)
    parser.add_argument("--sparse-k", type=int, default=40)
    parser.add_argument("--alias-k", type=int, default=12)
    parser.add_argument("--ppr-k", type=int, default=80)
    parser.add_argument("--rerank-k", type=int, default=20)
    parser.add_argument("--dense-max-length", type=int, default=512)
    parser.add_argument("--dense-num-threads", type=int, default=4)
    parser.add_argument("--reranker-max-length", type=int, default=512)
    parser.add_argument("--ppr-alpha", type=float, default=0.18)
    parser.add_argument("--ppr-iterations", type=int, default=18)
    parser.add_argument("--ppr-max-path-hops", type=int, default=3)
    parser.add_argument("--doc-types", default="", help="Comma-separated doc types: chunk,kg_node,question")
    parser.add_argument("--source-layers", default="", help="Comma-separated source layers.")
    parser.add_argument("--no-dense", action="store_true", help="Disable dense retrieval for candidate debugging.")
    parser.add_argument(
        "--candidate-only-debug",
        action="store_true",
        help="Return unre-ranked candidates only. This is not a final retrieval result.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = SolverConfig(
        kag_dir=args.kag_dir,
        embedding_model_dir=args.embedding_model_dir,
        reranker_model_dir=args.reranker_model_dir,
        enable_dense=not args.no_dense,
        require_reranker=not args.candidate_only_debug,
        dense_max_length=args.dense_max_length,
        dense_num_threads=args.dense_num_threads,
        reranker_max_length=args.reranker_max_length,
        ppr_alpha=args.ppr_alpha,
        ppr_iterations=args.ppr_iterations,
        ppr_top_k=args.ppr_k,
        ppr_max_path_hops=args.ppr_max_path_hops,
    )
    solver = KAGSolver(config)
    doc_types = parse_csv_set(args.doc_types)
    source_layers = parse_csv_set(args.source_layers)
    if args.candidate_only_debug:
        pack = solver.retrieve_candidates(
            args.query,
            dense_k=args.dense_k,
            sparse_k=args.sparse_k,
            alias_k=args.alias_k,
            ppr_k=args.ppr_k,
            doc_types=doc_types,
            source_layers=source_layers,
        )
        payload = {
            "query": args.query,
            "debug_only": True,
            "warning": "Candidates are not final retrieval results because bge-reranker-v2-m3 was not applied.",
            "retrieval_trace": {
                "operators": ["alias", "bm25", "dense_bge_m3" if not args.no_dense else "dense_disabled", "graph_ppr"],
                "alias_candidates": len(pack["aliases"]),
                "sparse_candidates": len(pack["sparse"]),
                "dense_candidates": len(pack["dense"]),
                "graph_seed_count": len(pack["seeds"]),
                "graph_candidates": len(pack["ppr"]),
                "candidate_count": len(pack["candidates"]),
                "gated_candidate_count": len(pack["gated_candidates"]),
            },
            "candidates": [
                {key: value for key, value in item.items() if not key.startswith("_")}
                for item in pack["gated_candidates"][: args.top_k]
            ],
        }
    else:
        payload = solver.query(
            args.query,
            task_type=args.task_type,
            top_k=args.top_k,
            dense_k=args.dense_k,
            sparse_k=args.sparse_k,
            alias_k=args.alias_k,
            ppr_k=args.ppr_k,
            rerank_k=args.rerank_k,
            doc_types=doc_types,
            source_layers=source_layers,
        )
    output = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.buffer.write(output.encode("utf-8", errors="replace"))
    sys.stdout.buffer.write(b"\n")


if __name__ == "__main__":
    main()
