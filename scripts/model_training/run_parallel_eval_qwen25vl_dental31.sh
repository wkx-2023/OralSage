#!/usr/bin/env bash

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODEL_ID="${MODEL_ID:-$PROJECT_ROOT/models/base_model/Qwen2.5-VL-7B-Instruct}"
ADAPTER_DIR="${ADAPTER_DIR:?ADAPTER_DIR is required}"
TEST_FILE="${TEST_FILE:-$PROJECT_ROOT/prepared_data/dental31_vlm/noaug/test_mixed.jsonl}"
OUTPUT_DIR="${OUTPUT_DIR:?OUTPUT_DIR is required}"
MAX_SAMPLES="${MAX_SAMPLES:-512}"
MAX_NEW_TOKENS="${MAX_NEW_TOKENS:-512}"
BATCH_SIZE="${BATCH_SIZE:-2}"
PROGRESS_EVERY="${PROGRESS_EVERY:-1}"
NUM_SHARDS="${NUM_SHARDS:-2}"
PYTHON_BIN="${PYTHON_BIN:-/home/linux/.venvs/hulumed/bin/python}"

mkdir -p "$OUTPUT_DIR"

run_shard() {
  local shard_index="$1"
  local visible="$2"
  local shard_dir="$OUTPUT_DIR/shard_${shard_index}"
  mkdir -p "$shard_dir"
  echo "[parallel-eval] starting shard ${shard_index}/${NUM_SHARDS} on GPU ${visible}"
  CUDA_VISIBLE_DEVICES="$visible" "$PYTHON_BIN" "$PROJECT_ROOT/scripts/eval_qwen25vl_dental31_lora.py" \
    --model-id "$MODEL_ID" \
    --adapter-dir "$ADAPTER_DIR" \
    --test-file "$TEST_FILE" \
    --output-dir "$shard_dir" \
    --max-samples "$MAX_SAMPLES" \
    --max-new-tokens "$MAX_NEW_TOKENS" \
    --batch-size "$BATCH_SIZE" \
    --progress-every "$PROGRESS_EVERY" \
    --num-shards "$NUM_SHARDS" \
    --shard-index "$shard_index" \
    --local-files-only
}

run_shard 0 0 > "$OUTPUT_DIR/shard_0.log" 2>&1 &
PID0=$!
run_shard 1 1 > "$OUTPUT_DIR/shard_1.log" 2>&1 &
PID1=$!

wait "$PID0"
wait "$PID1"

"$PYTHON_BIN" "$PROJECT_ROOT/scripts/merge_eval_shards.py" \
  --output-dir "$OUTPUT_DIR" \
  --shard-dirs "$OUTPUT_DIR/shard_0" "$OUTPUT_DIR/shard_1"
