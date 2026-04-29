from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.services.retrieval import search_knowledge_base


DEFAULT_CASES = PROJECT_ROOT / "data" / "manifests" / "retrieval_eval_cases.jsonl"


def load_cases(path: Path) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    if not path.exists():
        return cases
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            cases.append(json.loads(line))
    return cases


def hit_terms(text: str, terms: list[str]) -> list[str]:
    haystack = text.lower()
    return [term for term in terms if str(term).lower() in haystack]


def evaluate(cases: list[dict[str, Any]], top_k: int) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    term_hits = 0
    type_hits = 0
    reciprocal_ranks: list[float] = []

    for case in cases:
        query = str(case.get("query") or "")
        expected_terms = [str(item) for item in case.get("expected_terms") or []]
        expected_types = {str(item) for item in case.get("expected_source_types") or []}
        results = search_knowledge_base(query, top_k)
        first_term_rank = None
        first_type_rank = None
        result_rows = []
        for rank, chunk in enumerate(results, 1):
            text = f"{chunk.title}\n{chunk.text}"
            matched_terms = hit_terms(text, expected_terms)
            type_match = chunk.source_type in expected_types if expected_types else False
            if matched_terms and first_term_rank is None:
                first_term_rank = rank
            if type_match and first_type_rank is None:
                first_type_rank = rank
            result_rows.append(
                {
                    "rank": rank,
                    "id": chunk.id,
                    "source_type": chunk.source_type,
                    "title": chunk.title,
                    "score": round(float(chunk.score or 0.0), 4),
                    "matched_terms": matched_terms,
                    "channels": (chunk.metadata or {}).get("retrieval_channels") or [],
                }
            )
        if first_term_rank:
            term_hits += 1
            reciprocal_ranks.append(1 / first_term_rank)
        else:
            reciprocal_ranks.append(0.0)
        if first_type_rank:
            type_hits += 1
        rows.append(
            {
                "id": case.get("id"),
                "query": query,
                "term_hit": bool(first_term_rank),
                "term_rank": first_term_rank,
                "source_type_hit": bool(first_type_rank),
                "source_type_rank": first_type_rank,
                "results": result_rows,
            }
        )

    total = max(len(cases), 1)
    return {
        "total": len(cases),
        "top_k": top_k,
        "term_recall_at_k": round(term_hits / total, 4),
        "source_type_recall_at_k": round(type_hits / total, 4),
        "mrr": round(sum(reciprocal_ranks) / total, 4),
        "cases": rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate local retrieval recall on a small QA-oriented case set.")
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--top-k", type=int, default=8)
    parser.add_argument("--output", type=Path, default=PROJECT_ROOT / "data" / "processed" / "retrieval_eval_report.json")
    args = parser.parse_args()

    cases = load_cases(args.cases)
    report = evaluate(cases, args.top_k)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "cases"}, ensure_ascii=False, indent=2))
    print(str(args.output))


if __name__ == "__main__":
    main()
