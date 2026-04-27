#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import random
import re
import string
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import torch
from peft import PeftModel
from PIL import Image
from transformers import AutoModelForVision2Seq, AutoProcessor


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MODEL_ID = str(PROJECT_ROOT / "models" / "base_model" / "Qwen2.5-VL-7B-Instruct")
DEFAULT_TEST_FILE = PROJECT_ROOT / "prepared_data" / "dental31_vlm" / "noaug" / "test_mixed.jsonl"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "results" / "qwen25vl_dental31_structured_eval"


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


def normalize_text(text: str) -> str:
    lowered = text.lower().strip()
    translator = str.maketrans("", "", string.punctuation)
    lowered = lowered.translate(translator)
    return " ".join(lowered.split())


def token_f1(prediction: str, reference: str) -> float:
    pred_tokens = normalize_text(prediction).split()
    ref_tokens = normalize_text(reference).split()
    if not pred_tokens and not ref_tokens:
        return 1.0
    if not pred_tokens or not ref_tokens:
        return 0.0
    counts: dict[str, int] = {}
    for token in pred_tokens:
        counts[token] = counts.get(token, 0) + 1
    overlap = 0
    for token in ref_tokens:
        if counts.get(token, 0) > 0:
            overlap += 1
            counts[token] -= 1
    if overlap == 0:
        return 0.0
    precision = overlap / len(pred_tokens)
    recall = overlap / len(ref_tokens)
    return 2 * precision * recall / (precision + recall)


def lcs_length(a: list[str], b: list[str]) -> int:
    if not a or not b:
        return 0
    dp = [0] * (len(b) + 1)
    for token_a in a:
        prev = 0
        for idx, token_b in enumerate(b, start=1):
            current = dp[idx]
            if token_a == token_b:
                dp[idx] = prev + 1
            else:
                dp[idx] = max(dp[idx], dp[idx - 1])
            prev = current
    return dp[-1]


def rouge_l_f1(prediction: str, reference: str) -> float:
    pred_tokens = normalize_text(prediction).split()
    ref_tokens = normalize_text(reference).split()
    if not pred_tokens and not ref_tokens:
        return 1.0
    if not pred_tokens or not ref_tokens:
        return 0.0
    lcs = lcs_length(pred_tokens, ref_tokens)
    if lcs == 0:
        return 0.0
    precision = lcs / len(pred_tokens)
    recall = lcs / len(ref_tokens)
    return 2 * precision * recall / (precision + recall)


def clean_user_prompt(text: str) -> str:
    return text.replace("<image>\n", "", 1).replace("<image>", "", 1).strip()


def build_prompt(processor: Any, row: dict[str, Any]) -> tuple[str, Image.Image]:
    image_path = Path(row["images"][0])
    if not image_path.is_absolute():
        image_path = PROJECT_ROOT / image_path
    image = Image.open(image_path).convert("RGB")
    user_text = clean_user_prompt(row["messages"][0]["content"])
    prompt_messages = [
        {
            "role": "user",
            "content": [
                {"type": "image"},
                {"type": "text", "text": user_text},
            ],
        }
    ]
    prompt_text = processor.apply_chat_template(
        prompt_messages,
        tokenize=False,
        add_generation_prompt=True,
    )
    return prompt_text, image


def extract_json_array(text: str) -> list[dict[str, Any]] | None:
    text = text.strip()
    fence_match = re.search(r"```(?:json)?\s*(\[[\s\S]*\])\s*```", text)
    candidate = fence_match.group(1) if fence_match else None
    if candidate is None:
        start = text.find("[")
        end = text.rfind("]")
        if start != -1 and end != -1 and end > start:
            candidate = text[start:end + 1]
    if candidate is None:
        return None
    try:
        parsed = json.loads(candidate)
    except Exception:
        return None
    if not isinstance(parsed, list):
        return None
    normalized: list[dict[str, Any]] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        label_en = str(item.get("label_en", "")).strip()
        bbox = item.get("bbox_2d", [])
        if not label_en or not isinstance(bbox, list) or len(bbox) != 4:
            continue
        try:
            bbox = [float(x) for x in bbox]
        except Exception:
            continue
        normalized.append(
            {
                "label_en": label_en,
                "bbox_2d": bbox,
            }
        )
    return normalized


def box_iou(box_a: list[float], box_b: list[float]) -> float:
    ax1, ay1, ax2, ay2 = box_a
    bx1, by1, bx2, by2 = box_b
    inter_x1 = max(ax1, bx1)
    inter_y1 = max(ay1, by1)
    inter_x2 = min(ax2, bx2)
    inter_y2 = min(ay2, by2)
    inter_w = max(0.0, inter_x2 - inter_x1)
    inter_h = max(0.0, inter_y2 - inter_y1)
    inter_area = inter_w * inter_h
    area_a = max(0.0, ax2 - ax1) * max(0.0, ay2 - ay1)
    area_b = max(0.0, bx2 - bx1) * max(0.0, by2 - by1)
    union = area_a + area_b - inter_area
    if union <= 0:
        return 0.0
    return inter_area / union


def multiset_label_f1(pred_items: list[dict[str, Any]], ref_items: list[dict[str, Any]]) -> tuple[float, float, float]:
    pred_counts = Counter(item["label_en"] for item in pred_items)
    ref_counts = Counter(item["label_en"] for item in ref_items)
    overlap = 0
    for label, count in pred_counts.items():
        overlap += min(count, ref_counts.get(label, 0))
    pred_total = sum(pred_counts.values())
    ref_total = sum(ref_counts.values())
    if pred_total == 0 and ref_total == 0:
        return 1.0, 1.0, 1.0
    if pred_total == 0 or ref_total == 0:
        return 0.0, 0.0, 0.0
    precision = overlap / pred_total
    recall = overlap / ref_total
    if precision + recall == 0:
        return precision, recall, 0.0
    return precision, recall, 2 * precision * recall / (precision + recall)


def match_by_label_and_iou(
    pred_items: list[dict[str, Any]],
    ref_items: list[dict[str, Any]],
    iou_threshold: float,
) -> tuple[int, int, int, list[float]]:
    used_pred = set()
    used_ref = set()
    matched_ious: list[float] = []

    candidates: list[tuple[float, int, int]] = []
    for pred_idx, pred in enumerate(pred_items):
        for ref_idx, ref in enumerate(ref_items):
            if pred["label_en"] != ref["label_en"]:
                continue
            iou = box_iou(pred["bbox_2d"], ref["bbox_2d"])
            if iou >= iou_threshold:
                candidates.append((iou, pred_idx, ref_idx))
    candidates.sort(reverse=True, key=lambda x: x[0])

    for iou, pred_idx, ref_idx in candidates:
        if pred_idx in used_pred or ref_idx in used_ref:
            continue
        used_pred.add(pred_idx)
        used_ref.add(ref_idx)
        matched_ious.append(iou)

    tp = len(used_pred)
    fp = len(pred_items) - tp
    fn = len(ref_items) - tp
    return tp, fp, fn, matched_ious


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Structured evaluation for Qwen2.5-VL Dental31.")
    parser.add_argument("--model-id", default=DEFAULT_MODEL_ID)
    parser.add_argument("--adapter-dir", default="")
    parser.add_argument("--test-file", default=str(DEFAULT_TEST_FILE))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--max-samples", type=int, default=256)
    parser.add_argument("--max-new-tokens", type=int, default=384)
    parser.add_argument("--batch-size", type=int, default=2)
    parser.add_argument("--progress-every", type=int, default=8)
    parser.add_argument("--num-shards", type=int, default=1)
    parser.add_argument("--shard-index", type=int, default=0)
    parser.add_argument("--random-sample", action="store_true")
    parser.add_argument("--sample-seed", type=int, default=42)
    parser.add_argument("--local-files-only", action="store_true")
    parser.add_argument("--iou-threshold", type=float, default=0.5)
    return parser


def main() -> int:
    args = build_arg_parser().parse_args()
    rows = read_jsonl(Path(args.test_file))
    if args.random_sample:
        rng = random.Random(args.sample_seed)
        rng.shuffle(rows)
    if args.max_samples > 0:
        rows = rows[: args.max_samples]
    if args.num_shards > 1:
        rows = [row for idx, row in enumerate(rows) if idx % args.num_shards == args.shard_index]

    processor = AutoProcessor.from_pretrained(
        args.model_id,
        trust_remote_code=True,
        local_files_only=args.local_files_only,
        use_fast=False,
    )
    base_model = AutoModelForVision2Seq.from_pretrained(
        args.model_id,
        trust_remote_code=True,
        local_files_only=args.local_files_only,
        torch_dtype=torch.bfloat16,
        attn_implementation="sdpa",
        device_map="auto",
        low_cpu_mem_usage=True,
    )
    if args.adapter_dir:
        model = PeftModel.from_pretrained(
            base_model,
            args.adapter_dir,
            local_files_only=True,
        )
    else:
        model = base_model
    model.eval()
    device = next(model.parameters()).device

    predictions: list[dict[str, Any]] = []
    summary_f1_scores: list[float] = []
    summary_rouge_scores: list[float] = []

    parse_success = 0
    exact_structured_match = 0
    total_pred_targets = 0
    total_ref_targets = 0
    total_tp = 0
    total_fp = 0
    total_fn = 0
    matched_ious_all: list[float] = []
    label_f1_scores: list[float] = []
    count_mae_scores: list[float] = []

    start_time = time.time()
    total_rows = len(rows)

    for batch_idx, batch_start in enumerate(range(0, total_rows, args.batch_size), start=1):
        batch_rows = rows[batch_start: batch_start + args.batch_size]
        prompts: list[str] = []
        images: list[Image.Image] = []
        for row in batch_rows:
            prompt_text, image = build_prompt(processor, row)
            prompts.append(prompt_text)
            images.append(image)

        model_inputs = processor(
            text=prompts,
            images=images,
            padding=True,
            return_tensors="pt",
        )
        prompt_lengths = model_inputs["attention_mask"].sum(dim=1).tolist()
        model_inputs = {
            key: value.to(device) if isinstance(value, torch.Tensor) else value
            for key, value in model_inputs.items()
        }

        with torch.inference_mode():
            generated = model.generate(
                **model_inputs,
                max_new_tokens=args.max_new_tokens,
                do_sample=False,
                use_cache=True,
                pad_token_id=processor.tokenizer.eos_token_id,
            )

        for row_index, row in enumerate(batch_rows):
            generated_ids = generated[row_index, int(prompt_lengths[row_index]):]
            prediction_text = processor.batch_decode(
                [generated_ids],
                skip_special_tokens=True,
                clean_up_tokenization_spaces=False,
            )[0].strip()
            reference_text = row["messages"][1]["content"].strip()
            task_name = row.get("task", "unknown")

            sample_record: dict[str, Any] = {
                "id": row.get("id"),
                "task": task_name,
                "image": row["images"][0],
                "reference": reference_text,
                "prediction": prediction_text,
            }

            if task_name in {"detection_json", "rare_focus_json"}:
                ref_items = extract_json_array(reference_text) or []
                pred_items = extract_json_array(prediction_text)
                parsed_ok = pred_items is not None
                if parsed_ok:
                    parse_success += 1
                else:
                    pred_items = []

                total_pred_targets += len(pred_items)
                total_ref_targets += len(ref_items)
                label_p, label_r, label_f1 = multiset_label_f1(pred_items, ref_items)
                tp, fp, fn, matched_ious = match_by_label_and_iou(pred_items, ref_items, args.iou_threshold)
                total_tp += tp
                total_fp += fp
                total_fn += fn
                matched_ious_all.extend(matched_ious)
                count_mae = abs(len(pred_items) - len(ref_items))
                count_mae_scores.append(float(count_mae))
                label_f1_scores.append(label_f1)
                if parsed_ok and fp == 0 and fn == 0 and tp == len(ref_items):
                    exact_structured_match += 1

                sample_record.update(
                    {
                        "parsed_ok": parsed_ok,
                        "reference_items": ref_items,
                        "prediction_items": pred_items,
                        "label_f1": label_f1,
                        "count_mae": count_mae,
                        "matched_tp": tp,
                        "matched_fp": fp,
                        "matched_fn": fn,
                        "matched_iou_mean": float(sum(matched_ious) / len(matched_ious)) if matched_ious else 0.0,
                    }
                )
            else:
                summary_f1 = token_f1(prediction_text, reference_text)
                summary_rouge = rouge_l_f1(prediction_text, reference_text)
                summary_f1_scores.append(summary_f1)
                summary_rouge_scores.append(summary_rouge)
                sample_record.update(
                    {
                        "summary_token_f1": summary_f1,
                        "summary_rouge_l_f1": summary_rouge,
                    }
                )

            predictions.append(sample_record)

        processed = min(batch_start + len(batch_rows), total_rows)
        if batch_idx % args.progress_every == 0 or processed == total_rows:
            elapsed = max(time.time() - start_time, 1e-6)
            struct_examples = max(1, sum(1 for item in predictions if item["task"] in {"detection_json", "rare_focus_json"}))
            summary_examples = max(1, sum(1 for item in predictions if item["task"] == "summary_zh"))
            print(
                f"[structured-eval] {processed}/{total_rows} | "
                f"{processed / elapsed:.2f} samples/s | "
                f"parse_rate={parse_success / struct_examples:.4f} | "
                f"label_f1={(sum(label_f1_scores) / len(label_f1_scores)) if label_f1_scores else 0.0:.4f} | "
                f"summary_f1={(sum(summary_f1_scores) / len(summary_f1_scores)) if summary_f1_scores else 0.0:.4f}",
                flush=True,
            )

    det_precision = total_tp / max(total_tp + total_fp, 1)
    det_recall = total_tp / max(total_tp + total_fn, 1)
    det_f1 = 0.0 if det_precision + det_recall == 0 else 2 * det_precision * det_recall / (det_precision + det_recall)

    metrics = {
        "num_examples": len(predictions),
        "structured": {
            "num_examples": sum(1 for row in predictions if row["task"] in {"detection_json", "rare_focus_json"}),
            "json_parse_rate": parse_success / max(sum(1 for row in predictions if row["task"] in {"detection_json", "rare_focus_json"}), 1),
            "exact_structured_match_rate": exact_structured_match / max(sum(1 for row in predictions if row["task"] in {"detection_json", "rare_focus_json"}), 1),
            "label_f1_mean": sum(label_f1_scores) / len(label_f1_scores) if label_f1_scores else 0.0,
            "count_mae_mean": sum(count_mae_scores) / len(count_mae_scores) if count_mae_scores else 0.0,
            "det_precision_iou50": det_precision,
            "det_recall_iou50": det_recall,
            "det_f1_iou50": det_f1,
            "matched_iou_mean": sum(matched_ious_all) / len(matched_ious_all) if matched_ious_all else 0.0,
            "avg_pred_targets_per_image": total_pred_targets / max(sum(1 for row in predictions if row["task"] in {"detection_json", "rare_focus_json"}), 1),
            "avg_ref_targets_per_image": total_ref_targets / max(sum(1 for row in predictions if row["task"] in {"detection_json", "rare_focus_json"}), 1),
        },
        "summary": {
            "num_examples": sum(1 for row in predictions if row["task"] == "summary_zh"),
            "token_f1_mean": sum(summary_f1_scores) / len(summary_f1_scores) if summary_f1_scores else 0.0,
            "rouge_l_f1_mean": sum(summary_rouge_scores) / len(summary_rouge_scores) if summary_rouge_scores else 0.0,
        },
        "adapter_dir": args.adapter_dir,
        "test_file": args.test_file,
        "iou_threshold": args.iou_threshold,
        "num_shards": args.num_shards,
        "shard_index": args.shard_index,
        "random_sample": args.random_sample,
        "sample_seed": args.sample_seed,
    }

    output_dir = Path(args.output_dir)
    write_json(output_dir / "structured_metrics.json", metrics)
    write_jsonl(output_dir / "structured_predictions.jsonl", predictions)
    print(json.dumps(metrics, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
