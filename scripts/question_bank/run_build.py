from __future__ import annotations

import argparse
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.services.question_bank_builder.workflow import run_build  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build the oral medicine question bank workflow.")
    parser.add_argument(
        "--source-dir",
        default="data/original_question",
        help="Directory containing paired 题目/解析 .docx files.",
    )
    parser.add_argument(
        "--intermediate-dir",
        default="data/intermediate/question_bank_builder",
        help="Directory for document blocks and raw LLM outputs.",
    )
    parser.add_argument(
        "--output-dir",
        default="data/question_bank",
        help="Directory for final question_bank.json/jsonl.",
    )
    parser.add_argument(
        "--review-dir",
        default="data/review_required",
        help="Directory for review_required.jsonl.",
    )
    parser.add_argument(
        "--reports-dir",
        default="reports/question_bank",
        help="Directory for build and validation reports.",
    )
    parser.add_argument(
        "--kg-draft-dir",
        default="data/oralpath_kg/manual_drafts",
        help="Directory containing teammate KG draft JSON files.",
    )
    parser.add_argument(
        "--ingest-only",
        action="store_true",
        help="Only read docx files into document blocks; do not call Qwen.",
    )
    parser.add_argument(
        "--limit-subjects",
        type=int,
        default=None,
        help="Process only the first N subject pairs for smoke testing.",
    )
    parser.add_argument(
        "--chunk-chars",
        type=int,
        default=20000,
        help="Maximum source text characters per Qwen extraction window.",
    )
    parser.add_argument(
        "--chunk-overlap-blocks",
        type=int,
        default=10,
        help="Number of source blocks overlapped between adjacent Qwen extraction windows.",
    )
    parser.add_argument(
        "--limit-chunks",
        type=int,
        default=None,
        help="Debug option: process only the first N extraction chunks per document.",
    )
    parser.add_argument(
        "--answer-skip-chunks",
        type=int,
        default=0,
        help="Debug option: skip the first N answer-document chunks before extraction.",
    )
    parser.add_argument(
        "--max-subject-workers",
        type=int,
        default=1,
        help="Maximum subject jobs to run concurrently.",
    )
    parser.add_argument(
        "--max-qwen-workers",
        type=int,
        default=1,
        help="Maximum Qwen chunk calls to run concurrently within each subject.",
    )
    parser.add_argument(
        "--require-kg-match",
        action="store_true",
        help="Require every accepted question to match an existing KG core immediately.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    result = run_build(
        source_dir=(PROJECT_ROOT / args.source_dir).resolve(),
        intermediate_dir=(PROJECT_ROOT / args.intermediate_dir).resolve(),
        output_dir=(PROJECT_ROOT / args.output_dir).resolve(),
        review_dir=(PROJECT_ROOT / args.review_dir).resolve(),
        reports_dir=(PROJECT_ROOT / args.reports_dir).resolve(),
        kg_draft_dir=(PROJECT_ROOT / args.kg_draft_dir).resolve() if args.kg_draft_dir else None,
        ingest_only=args.ingest_only,
        limit_subjects=args.limit_subjects,
        chunk_chars=args.chunk_chars,
        chunk_overlap_blocks=args.chunk_overlap_blocks,
        limit_chunks=args.limit_chunks,
        answer_skip_chunks=args.answer_skip_chunks,
        max_subject_workers=args.max_subject_workers,
        max_qwen_workers=args.max_qwen_workers,
        require_kg_match=args.require_kg_match,
    )
    print("Question bank workflow finished.")
    print(f"Subjects: {len(result.get('doc_pairs', []))}")
    print(f"Accepted records: {len(result.get('valid_records', []))}")
    print(f"Review required: {len(result.get('review_required', []))}")


if __name__ == "__main__":
    main()
