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


def mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Merge structured eval shards.")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--shard-dirs", nargs="+", required=True)
    return parser


def main() -> int:
    args = build_arg_parser().parse_args()

    all_predictions: list[dict[str, Any]] = []
    for shard_dir_str in args.shard_dirs:
        shard_dir = Path(shard_dir_str)
        pred_path = shard_dir / "structured_predictions.jsonl"
        if pred_path.exists():
            all_predictions.extend(read_jsonl(pred_path))

    if not all_predictions:
        raise SystemExit("No structured prediction shards found.")

    all_predictions.sort(key=lambda row: row.get("id", ""))

    struct_rows = [row for row in all_predictions if row["task"] in {"detection_json", "rare_focus_json"}]
    summary_rows = [row for row in all_predictions if row["task"] == "summary_zh"]

    total_tp = sum(int(row.get("matched_tp", 0)) for row in struct_rows)
    total_fp = sum(int(row.get("matched_fp", 0)) for row in struct_rows)
    total_fn = sum(int(row.get("matched_fn", 0)) for row in struct_rows)

    det_precision = total_tp / max(total_tp + total_fp, 1)
    det_recall = total_tp / max(total_tp + total_fn, 1)
    det_f1 = 0.0 if det_precision + det_recall == 0 else 2 * det_precision * det_recall / (det_precision + det_recall)

    metrics = {
        "num_examples": len(all_predictions),
        "structured": {
            "num_examples": len(struct_rows),
            "json_parse_rate": mean([1.0 if row.get("parsed_ok") else 0.0 for row in struct_rows]),
            "exact_structured_match_rate": mean(
                [
                    1.0
                    if row.get("parsed_ok") and row.get("matched_fp", 0) == 0 and row.get("matched_fn", 0) == 0
                    else 0.0
                    for row in struct_rows
                ]
            ),
            "label_f1_mean": mean([float(row.get("label_f1", 0.0)) for row in struct_rows]),
            "count_mae_mean": mean([float(row.get("count_mae", 0.0)) for row in struct_rows]),
            "det_precision_iou50": det_precision,
            "det_recall_iou50": det_recall,
            "det_f1_iou50": det_f1,
            "matched_iou_mean": mean([float(row.get("matched_iou_mean", 0.0)) for row in struct_rows if row.get("matched_tp", 0) > 0]),
            "avg_pred_targets_per_image": mean([float(len(row.get("prediction_items", []))) for row in struct_rows]),
            "avg_ref_targets_per_image": mean([float(len(row.get("reference_items", []))) for row in struct_rows]),
        },
        "summary": {
            "num_examples": len(summary_rows),
            "token_f1_mean": mean([float(row.get("summary_token_f1", 0.0)) for row in summary_rows]),
            "rouge_l_f1_mean": mean([float(row.get("summary_rouge_l_f1", 0.0)) for row in summary_rows]),
        },
    }

    output_dir = Path(args.output_dir)
    write_json(output_dir / "structured_metrics.json", metrics)
    write_jsonl(output_dir / "structured_predictions.jsonl", all_predictions)
    print(json.dumps(metrics, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
