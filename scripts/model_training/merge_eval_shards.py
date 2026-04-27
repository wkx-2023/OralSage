#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Merge sharded eval outputs.")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--shard-dirs", nargs="+", required=True)
    return parser


def main() -> int:
    args = build_arg_parser().parse_args()
    all_predictions: list[dict[str, Any]] = []
    shard_metrics: list[dict[str, Any]] = []

    for shard_dir_str in args.shard_dirs:
        shard_dir = Path(shard_dir_str)
        metrics_path = shard_dir / "metrics.json"
        predictions_path = shard_dir / "predictions.jsonl"
        if metrics_path.exists():
            shard_metrics.append(json.loads(metrics_path.read_text(encoding="utf-8")))
        if predictions_path.exists():
            all_predictions.extend(read_jsonl(predictions_path))

    all_predictions.sort(key=lambda item: item.get("id", ""))
    if not all_predictions:
        raise SystemExit("No shard predictions found.")

    exact_match = sum(float(item["exact_match"]) for item in all_predictions) / len(all_predictions)
    token_f1 = sum(float(item["token_f1"]) for item in all_predictions) / len(all_predictions)
    rouge_l_f1 = sum(float(item["rouge_l_f1"]) for item in all_predictions) / len(all_predictions)

    task_groups: dict[str, list[dict[str, Any]]] = {}
    for item in all_predictions:
        task_groups.setdefault(item.get("task", "unknown"), []).append(item)

    task_breakdown = {}
    for task_name, rows in task_groups.items():
        task_breakdown[task_name] = {
            "num_examples": len(rows),
            "exact_match": sum(float(r["exact_match"]) for r in rows) / len(rows),
            "token_f1": sum(float(r["token_f1"]) for r in rows) / len(rows),
            "rouge_l_f1": sum(float(r["rouge_l_f1"]) for r in rows) / len(rows),
        }

    merged_summary = {
        "num_examples": len(all_predictions),
        "exact_match": exact_match,
        "token_f1": token_f1,
        "rouge_l_f1": rouge_l_f1,
        "task_breakdown": task_breakdown,
        "shard_metrics": shard_metrics,
    }

    output_dir = Path(args.output_dir)
    write_json(output_dir / "metrics.json", merged_summary)
    write_jsonl(output_dir / "predictions.jsonl", all_predictions)
    print(json.dumps(merged_summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
