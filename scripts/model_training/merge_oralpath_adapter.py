#!/usr/bin/env python3

from __future__ import annotations

import argparse
from pathlib import Path

import torch
from peft import PeftModel
from transformers import AutoModelForVision2Seq, AutoProcessor


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Merge OralPath LoRA adapter into a Qwen2.5-VL base model.")
    parser.add_argument("--base-model", required=True, help="Path to the Qwen2.5-VL-7B-Instruct base model.")
    parser.add_argument("--adapter-dir", required=True, help="Path to the OralPath final LoRA adapter directory.")
    parser.add_argument("--output-dir", required=True, help="Directory to save the merged OralPath model.")
    parser.add_argument(
        "--torch-dtype",
        default="bfloat16",
        choices=["auto", "float16", "bfloat16", "float32"],
        help="Torch dtype used during loading.",
    )
    parser.add_argument(
        "--device-map",
        default="auto",
        help='Device map for loading the base model. Use "auto", "cpu", or a valid transformers device_map string.',
    )
    parser.add_argument("--local-files-only", action="store_true", help="Only read local files.")
    return parser.parse_args()


def resolve_dtype(name: str):
    if name == "auto":
        return "auto"
    return getattr(torch, name)


def resolve_device_map(device_map: str):
    if device_map in {"auto", "cpu"}:
        return device_map
    return device_map


def main() -> None:
    args = parse_args()

    base_model = Path(args.base_model).expanduser().resolve()
    adapter_dir = Path(args.adapter_dir).expanduser().resolve()
    output_dir = Path(args.output_dir).expanduser().resolve()

    if not base_model.exists():
        raise FileNotFoundError(f"Base model path does not exist: {base_model}")
    if not adapter_dir.exists():
        raise FileNotFoundError(f"Adapter directory does not exist: {adapter_dir}")

    output_dir.mkdir(parents=True, exist_ok=True)

    dtype = resolve_dtype(args.torch_dtype)
    device_map = resolve_device_map(args.device_map)

    print(f"[1/4] Loading base model from: {base_model}")
    model = AutoModelForVision2Seq.from_pretrained(
        str(base_model),
        torch_dtype=dtype,
        device_map=device_map,
        local_files_only=args.local_files_only,
    )

    print(f"[2/4] Loading OralPath adapter from: {adapter_dir}")
    model = PeftModel.from_pretrained(
        model,
        str(adapter_dir),
        local_files_only=args.local_files_only,
    )

    print("[3/4] Merging OralPath adapter into base model")
    merged_model = model.merge_and_unload()

    print(f"[4/4] Saving merged OralPath model to: {output_dir}")
    merged_model.save_pretrained(str(output_dir), safe_serialization=True)

    processor = AutoProcessor.from_pretrained(
        str(adapter_dir),
        local_files_only=args.local_files_only,
    )
    processor.save_pretrained(str(output_dir))

    print("Merge complete.")


if __name__ == "__main__":
    main()
