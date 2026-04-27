#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import string
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

import torch
from peft import PeftModel
from PIL import Image
from transformers import AutoModelForVision2Seq, AutoProcessor


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MODEL_ID = str(PROJECT_ROOT / "models" / "base_model" / "Qwen2.5-VL-7B-Instruct")
DEFAULT_TEST_FILE = PROJECT_ROOT / "prepared_data" / "dental31_vlm" / "noaug" / "test_mixed.jsonl"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "results" / "qwen25vl_dental31_eval"


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


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Evaluate a Qwen2.5-VL Dental31 LoRA adapter.")
    parser.add_argument("--model-id", default=DEFAULT_MODEL_ID)
    parser.add_argument("--adapter-dir", required=True)
    parser.add_argument("--test-file", default=str(DEFAULT_TEST_FILE))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--max-samples", type=int, default=512)
    parser.add_argument("--max-new-tokens", type=int, default=512)
    parser.add_argument("--batch-size", type=int, default=2)
    parser.add_argument("--progress-every", type=int, default=1)
    parser.add_argument("--num-shards", type=int, default=1)
    parser.add_argument("--shard-index", type=int, default=0)
    parser.add_argument("--local-files-only", action="store_true")
    return parser


def main() -> int:
    args = build_arg_parser().parse_args()
    rows = read_jsonl(Path(args.test_file))
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
    model = PeftModel.from_pretrained(
        base_model,
        args.adapter_dir,
        local_files_only=True,
    )
    model.eval()

    predictions: list[dict[str, Any]] = []
    em_scores: list[float] = []
    f1_scores: list[float] = []
    rouge_scores: list[float] = []
    task_metrics: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    start_time = time.time()
    device = next(model.parameters()).device
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
            prediction = processor.batch_decode(
                [generated_ids],
                skip_special_tokens=True,
                clean_up_tokenization_spaces=False,
            )[0].strip()
            reference = row["messages"][1]["content"].strip()

            em = float(normalize_text(prediction) == normalize_text(reference))
            f1 = token_f1(prediction, reference)
            rouge = rouge_l_f1(prediction, reference)

            em_scores.append(em)
            f1_scores.append(f1)
            rouge_scores.append(rouge)
            task_name = row.get("task", "unknown")
            task_metrics[task_name]["exact_match"].append(em)
            task_metrics[task_name]["token_f1"].append(f1)
            task_metrics[task_name]["rouge_l_f1"].append(rouge)

            predictions.append(
                {
                    "id": row.get("id"),
                    "task": task_name,
                    "image": row["images"][0],
                    "reference": reference,
                    "prediction": prediction,
                    "exact_match": em,
                    "token_f1": f1,
                    "rouge_l_f1": rouge,
                }
            )

        processed = min(batch_start + len(batch_rows), total_rows)
        if batch_idx % args.progress_every == 0 or processed == total_rows:
            elapsed = max(time.time() - start_time, 1e-6)
            avg_f1 = sum(f1_scores) / len(f1_scores) if f1_scores else 0.0
            avg_rouge = sum(rouge_scores) / len(rouge_scores) if rouge_scores else 0.0
            print(
                f"[eval] {processed}/{total_rows} | "
                f"{processed / elapsed:.2f} samples/s | "
                f"avg_f1={avg_f1:.4f} | avg_rouge={avg_rouge:.4f}",
                flush=True,
            )

    task_summary = {}
    for task_name, metric_groups in task_metrics.items():
        task_summary[task_name] = {
            metric_name: float(sum(values) / len(values)) if values else 0.0
            for metric_name, values in metric_groups.items()
        }
        task_summary[task_name]["num_examples"] = len(metric_groups["exact_match"])

    summary = {
        "num_examples": len(predictions),
        "exact_match": float(sum(em_scores) / len(em_scores)) if em_scores else 0.0,
        "token_f1": float(sum(f1_scores) / len(f1_scores)) if f1_scores else 0.0,
        "rouge_l_f1": float(sum(rouge_scores) / len(rouge_scores)) if rouge_scores else 0.0,
        "task_breakdown": task_summary,
        "adapter_dir": args.adapter_dir,
        "test_file": args.test_file,
        "num_shards": args.num_shards,
        "shard_index": args.shard_index,
    }

    output_dir = Path(args.output_dir)
    write_json(output_dir / "metrics.json", summary)
    write_jsonl(output_dir / "predictions.jsonl", predictions)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
