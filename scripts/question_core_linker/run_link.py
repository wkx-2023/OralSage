from __future__ import annotations

import argparse
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.services.question_core_linker.workflow import run_linking  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Link cleaned question-bank records to reviewed KG core nodes.")
    parser.add_argument(
        "--question-bank",
        default="data/question_bank/question_bank.json",
        help="Input cleaned question bank JSON. This file is read only.",
    )
    parser.add_argument(
        "--kg-draft-dir",
        default="data/core/manual-kg-drafts",
        help="Reviewed KG draft directory containing subject folders and *.kg-draft.json files.",
    )
    parser.add_argument(
        "--output-dir",
        default="data/question_bank_core_linked",
        help="Directory for question_bank_with_kg_core.json/jsonl and indexes.",
    )
    parser.add_argument(
        "--review-dir",
        default="data/review_required/question_core_linker",
        help="Directory for core-linking review queue.",
    )
    parser.add_argument(
        "--reports-dir",
        default="reports/question_core_linker",
        help="Directory for summary reports.",
    )
    parser.add_argument(
        "--intermediate-dir",
        default="data/intermediate/question_core_linker",
        help="Directory for cached LLM outputs.",
    )
    parser.add_argument(
        "--book",
        default=None,
        help="Process only records whose subject_name equals this KG book/subject name.",
    )
    parser.add_argument(
        "--limit-records",
        type=int,
        default=None,
        help="Process only the first N question records for smoke testing.",
    )
    parser.add_argument(
        "--sample-stride",
        type=int,
        default=None,
        help="After optional --book filtering, process every Nth record. Use 5 to sample records 1, 6, 11, ...",
    )
    parser.add_argument(
        "--max-workers",
        type=int,
        default=1,
        help="Question-level concurrency. Use 1 for smoke tests.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    result = run_linking(
        question_bank_path=(PROJECT_ROOT / args.question_bank).resolve(),
        kg_draft_dir=(PROJECT_ROOT / args.kg_draft_dir).resolve(),
        output_dir=(PROJECT_ROOT / args.output_dir).resolve(),
        review_dir=(PROJECT_ROOT / args.review_dir).resolve(),
        reports_dir=(PROJECT_ROOT / args.reports_dir).resolve(),
        intermediate_dir=(PROJECT_ROOT / args.intermediate_dir).resolve(),
        book_name=args.book,
        limit_records=args.limit_records,
        sample_stride=args.sample_stride,
        max_workers=args.max_workers,
    )
    summary = result.get("summary", {})
    print("Question core linking workflow finished.")
    print(f"Processed questions: {summary.get('processed_questions')}")
    print(f"KG cores loaded: {summary.get('kg_core_count')}")
    print(f"Review required: {summary.get('review_required')}")
    print(f"Status counts: {summary.get('status_counts')}")
    outputs = summary.get("outputs") or {}
    for label, path in outputs.items():
        print(f"{label}: {path}")


if __name__ == "__main__":
    main()
