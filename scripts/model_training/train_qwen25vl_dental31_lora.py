#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import os
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import torch
from peft import LoraConfig, PeftModel, get_peft_model
from PIL import Image
from transformers import (
    AutoModelForVision2Seq,
    AutoProcessor,
    Trainer,
    TrainingArguments,
    TrainerCallback,
    set_seed,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TRAIN_FILE = str(PROJECT_ROOT / "prepared_data" / "dental31_vlm" / "noaug" / "train_mixed.jsonl")
DEFAULT_VALID_FILE = str(PROJECT_ROOT / "prepared_data" / "dental31_vlm" / "noaug" / "valid_mixed.jsonl")
DEFAULT_OUTPUT_DIR = str(PROJECT_ROOT / "runs" / "qwen25vl_dental31_lora")
DEFAULT_MODEL_ID = str(PROJECT_ROOT / "models" / "base_model" / "Qwen2.5-VL-7B-Instruct")


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


def maybe_limit(rows: list[dict[str, Any]], max_samples: int, seed: int) -> list[dict[str, Any]]:
    if max_samples <= 0 or len(rows) <= max_samples:
        return rows
    random.seed(seed)
    rows = rows[:]
    random.shuffle(rows)
    return rows[:max_samples]


class Dental31VLMDataset(torch.utils.data.Dataset):
    def __init__(self, rows: list[dict[str, Any]]):
        self.rows = rows

    def __len__(self) -> int:
        return len(self.rows)

    def __getitem__(self, index: int) -> dict[str, Any]:
        return self.rows[index]


def clean_user_prompt(text: str) -> str:
    return text.replace("<image>\n", "", 1).replace("<image>", "", 1).strip()


@dataclass
class Dental31Qwen25VLDataCollator:
    processor: Any
    max_length: int
    image_root: Path

    def _build_texts_and_image(self, row: dict[str, Any]) -> tuple[str, str, Image.Image]:
        image_value = row["images"][0]
        image_path = Path(image_value)
        if not image_path.is_absolute():
            image_path = self.image_root / image_path
        image = Image.open(image_path).convert("RGB")

        user_text = clean_user_prompt(row["messages"][0]["content"])
        assistant_text = row["messages"][1]["content"]

        prompt_messages = [
            {
                "role": "user",
                "content": [
                    {"type": "image"},
                    {"type": "text", "text": user_text},
                ],
            }
        ]
        full_messages = prompt_messages + [
            {
                "role": "assistant",
                "content": [{"type": "text", "text": assistant_text}],
            }
        ]

        prompt_text = self.processor.apply_chat_template(
            prompt_messages,
            tokenize=False,
            add_generation_prompt=True,
        )
        full_text = self.processor.apply_chat_template(
            full_messages,
            tokenize=False,
            add_generation_prompt=False,
        )
        return prompt_text, full_text, image

    def __call__(self, batch: list[dict[str, Any]]) -> dict[str, Any]:
        encoded_batch: list[dict[str, Any]] = []
        for row in batch:
            prompt_text, full_text, image = self._build_texts_and_image(row)
            processor_kwargs = {
                "return_tensors": "pt",
                "truncation": True,
                "max_length": self.max_length,
            }
            prompt_inputs = self.processor(
                text=[prompt_text],
                images=[image],
                **processor_kwargs,
            )
            full_inputs = self.processor(
                text=[full_text],
                images=[image],
                **processor_kwargs,
            )
            labels = full_inputs["input_ids"].clone()
            prompt_len = int(prompt_inputs["input_ids"].shape[1])
            labels[:, :prompt_len] = -100

            encoded_batch.append(
                {
                    "input_ids": full_inputs["input_ids"].squeeze(0),
                    "attention_mask": full_inputs["attention_mask"].squeeze(0),
                    "labels": labels.squeeze(0),
                    "pixel_values": full_inputs["pixel_values"],
                    "image_grid_thw": full_inputs["image_grid_thw"],
                }
            )

        pad_token_id = self.processor.tokenizer.pad_token_id
        if pad_token_id is None:
            pad_token_id = self.processor.tokenizer.eos_token_id

        max_seq_len = max(item["input_ids"].shape[0] for item in encoded_batch)
        batch_size = len(encoded_batch)

        input_ids = torch.full((batch_size, max_seq_len), pad_token_id, dtype=torch.long)
        attention_mask = torch.zeros((batch_size, max_seq_len), dtype=torch.long)
        labels = torch.full((batch_size, max_seq_len), -100, dtype=torch.long)

        for index, item in enumerate(encoded_batch):
            seq_len = item["input_ids"].shape[0]
            input_ids[index, :seq_len] = item["input_ids"]
            attention_mask[index, :seq_len] = item["attention_mask"]
            labels[index, :seq_len] = item["labels"]

        return {
            "input_ids": input_ids,
            "attention_mask": attention_mask,
            "labels": labels,
            "pixel_values": torch.cat([item["pixel_values"] for item in encoded_batch], dim=0),
            "image_grid_thw": torch.cat([item["image_grid_thw"] for item in encoded_batch], dim=0),
        }


class SaveSummaryCallback(TrainerCallback):
    def __init__(self, output_dir: Path):
        self.output_dir = output_dir
        self.started_at = time.time()

    def on_train_end(self, args, state, control, **kwargs):
        if not state.is_world_process_zero:
            return
        summary = {
            "global_step": state.global_step,
            "train_runtime_seconds": round(time.time() - self.started_at, 2),
            "best_model_checkpoint": state.best_model_checkpoint,
            "best_metric": state.best_metric,
            "log_history_tail": state.log_history[-20:],
        }
        write_json(self.output_dir / "training_summary.json", summary)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Train Qwen2.5-VL on Dental31 VLM data with LoRA.")
    parser.add_argument("--model-id", default=DEFAULT_MODEL_ID)
    parser.add_argument("--train-file", default=DEFAULT_TRAIN_FILE)
    parser.add_argument("--valid-file", default=DEFAULT_VALID_FILE)
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--image-root", default=str(PROJECT_ROOT))
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--max-length", type=int, default=4608)
    parser.add_argument("--num-train-epochs", type=float, default=1.0)
    parser.add_argument("--learning-rate", type=float, default=7e-5)
    parser.add_argument("--warmup-ratio", type=float, default=0.03)
    parser.add_argument("--weight-decay", type=float, default=0.0)
    parser.add_argument("--per-device-train-batch-size", type=int, default=1)
    parser.add_argument("--per-device-eval-batch-size", type=int, default=1)
    parser.add_argument("--gradient-accumulation-steps", type=int, default=4)
    parser.add_argument("--logging-steps", type=int, default=10)
    parser.add_argument("--eval-steps", type=int, default=500)
    parser.add_argument("--save-steps", type=int, default=500)
    parser.add_argument("--save-total-limit", type=int, default=3)
    parser.add_argument("--dataloader-num-workers", type=int, default=4)
    parser.add_argument("--resume-from-checkpoint", default="")
    parser.add_argument("--max-train-samples", type=int, default=-1)
    parser.add_argument("--max-eval-samples", type=int, default=256)
    parser.add_argument("--lora-r", type=int, default=32)
    parser.add_argument("--lora-alpha", type=int, default=64)
    parser.add_argument("--lora-dropout", type=float, default=0.05)
    parser.add_argument("--attn-implementation", default="sdpa")
    parser.add_argument("--local-files-only", action="store_true")
    parser.add_argument("--init-adapter-dir", default="")
    parser.add_argument("--merge-after-train", action="store_true")
    parser.add_argument("--merged-output-dir", default="")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main() -> int:
    args = build_arg_parser().parse_args()

    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    torch.backends.cuda.matmul.allow_tf32 = True
    torch.backends.cudnn.allow_tf32 = True
    set_seed(args.seed)

    train_rows = maybe_limit(read_jsonl(Path(args.train_file)), args.max_train_samples, args.seed)
    eval_rows = maybe_limit(read_jsonl(Path(args.valid_file)), args.max_eval_samples, args.seed)

    processor = AutoProcessor.from_pretrained(
        args.model_id,
        trust_remote_code=True,
        local_files_only=args.local_files_only,
        use_fast=False,
    )
    collator = Dental31Qwen25VLDataCollator(
        processor=processor,
        max_length=args.max_length,
        image_root=Path(args.image_root),
    )

    if args.dry_run:
        sample_batch = collator([train_rows[0]])
        summary = {
            "input_ids_shape": tuple(sample_batch["input_ids"].shape),
            "attention_mask_shape": tuple(sample_batch["attention_mask"].shape),
            "labels_shape": tuple(sample_batch["labels"].shape),
            "pixel_values_shape": tuple(sample_batch["pixel_values"].shape),
            "image_grid_thw_shape": tuple(sample_batch["image_grid_thw"].shape),
            "supervised_tokens": int((sample_batch["labels"] != -100).sum().item()),
        }
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0

    train_dataset = Dental31VLMDataset(train_rows)
    eval_dataset = Dental31VLMDataset(eval_rows)

    model = AutoModelForVision2Seq.from_pretrained(
        args.model_id,
        trust_remote_code=True,
        local_files_only=args.local_files_only,
        torch_dtype=torch.bfloat16,
        attn_implementation=args.attn_implementation,
        low_cpu_mem_usage=True,
    )
    model.config.use_cache = False
    if hasattr(model, "enable_input_require_grads"):
        model.enable_input_require_grads()

    if args.init_adapter_dir:
        model = PeftModel.from_pretrained(
            model,
            args.init_adapter_dir,
            is_trainable=True,
            local_files_only=True,
        )
    else:
        lora_config = LoraConfig(
            r=args.lora_r,
            lora_alpha=args.lora_alpha,
            lora_dropout=args.lora_dropout,
            target_modules=["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"],
            task_type="CAUSAL_LM",
            bias="none",
        )
        model = get_peft_model(model, lora_config)
    if hasattr(model, "print_trainable_parameters"):
        model.print_trainable_parameters()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    write_json(
        output_dir / "run_config.json",
        {
            "model_id": args.model_id,
            "train_file": args.train_file,
            "valid_file": args.valid_file,
            "train_samples": len(train_rows),
            "eval_samples": len(eval_rows),
            "max_length": args.max_length,
            "num_train_epochs": args.num_train_epochs,
            "learning_rate": args.learning_rate,
            "gradient_accumulation_steps": args.gradient_accumulation_steps,
            "per_device_train_batch_size": args.per_device_train_batch_size,
            "per_device_eval_batch_size": args.per_device_eval_batch_size,
            "lora_r": args.lora_r,
            "lora_alpha": args.lora_alpha,
            "lora_dropout": args.lora_dropout,
            "init_adapter_dir": args.init_adapter_dir,
        },
    )

    optim_name = "adamw_torch_fused" if torch.cuda.is_available() else "adamw_torch"

    training_args = TrainingArguments(
        output_dir=str(output_dir),
        num_train_epochs=args.num_train_epochs,
        learning_rate=args.learning_rate,
        warmup_ratio=args.warmup_ratio,
        weight_decay=args.weight_decay,
        per_device_train_batch_size=args.per_device_train_batch_size,
        per_device_eval_batch_size=args.per_device_eval_batch_size,
        gradient_accumulation_steps=args.gradient_accumulation_steps,
        logging_steps=args.logging_steps,
        logging_strategy="steps",
        logging_first_step=True,
        eval_strategy="steps",
        eval_steps=args.eval_steps,
        save_strategy="steps",
        save_steps=args.save_steps,
        save_total_limit=args.save_total_limit,
        bf16=True,
        tf32=True,
        dataloader_num_workers=args.dataloader_num_workers,
        dataloader_pin_memory=True,
        dataloader_persistent_workers=args.dataloader_num_workers > 0,
        remove_unused_columns=False,
        report_to=[],
        optim=optim_name,
        lr_scheduler_type="cosine",
        max_grad_norm=1.0,
        ddp_find_unused_parameters=False,
        gradient_checkpointing=True,
        gradient_checkpointing_kwargs={"use_reentrant": False},
        load_best_model_at_end=True,
        metric_for_best_model="eval_loss",
        greater_is_better=False,
        save_safetensors=True,
        disable_tqdm=False,
        label_names=["labels"],
    )

    trainer = Trainer(
        model=model,
        args=training_args,
        train_dataset=train_dataset,
        eval_dataset=eval_dataset,
        data_collator=collator,
        processing_class=processor,
        callbacks=[SaveSummaryCallback(output_dir)],
    )

    trainer.train(resume_from_checkpoint=args.resume_from_checkpoint or None)

    final_adapter_dir = output_dir / "final_adapter"
    trainer.model.save_pretrained(final_adapter_dir)
    processor.save_pretrained(final_adapter_dir)
    trainer.save_state()

    if args.merge_after_train and trainer.is_world_process_zero():
        merged_output_dir = Path(args.merged_output_dir) if args.merged_output_dir else output_dir / "merged_model"
        merged_model = trainer.model.merge_and_unload()
        merged_model.save_pretrained(
            merged_output_dir,
            safe_serialization=True,
            max_shard_size="4GB",
        )
        processor.save_pretrained(merged_output_dir)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
