#!/usr/bin/env bash

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STAGE="${STAGE:-main}"
NPROC_PER_NODE="${NPROC_PER_NODE:-2}"
MODEL_ID="${MODEL_ID:-$PROJECT_ROOT/models/base_model/Qwen2.5-VL-7B-Instruct}"
HF_HOME="${HF_HOME:-/data/huggingface}"
OUTPUT_ROOT="${OUTPUT_ROOT:-$PROJECT_ROOT/runs}"
RUN_NAME="${RUN_NAME:-qwen25vl_dental31_${STAGE}_$(date +%Y%m%d_%H%M%S)}"
OUTPUT_DIR="${OUTPUT_DIR:-$OUTPUT_ROOT/$RUN_NAME}"
LOG_DIR="${LOG_DIR:-$OUTPUT_DIR}"
LOG_FILE="${LOG_FILE:-$LOG_DIR/train.log}"
TORCHRUN_BIN="${TORCHRUN_BIN:-/home/linux/.venvs/hulumed/bin/torchrun}"

export HF_HOME
export TRANSFORMERS_CACHE="${TRANSFORMERS_CACHE:-$HF_HOME/hub}"
export TOKENIZERS_PARALLELISM=false
export OMP_NUM_THREADS="${OMP_NUM_THREADS:-8}"
export CUDA_VISIBLE_DEVICES="${CUDA_VISIBLE_DEVICES:-0,1}"
export PYTORCH_CUDA_ALLOC_CONF="${PYTORCH_CUDA_ALLOC_CONF:-expandable_segments:True}"

TRAIN_FILE=""
VALID_FILE="$PROJECT_ROOT/prepared_data/dental31_vlm/noaug/valid_mixed.jsonl"
NUM_EPOCHS="${NUM_EPOCHS:-}"
LEARNING_RATE="${LEARNING_RATE:-}"
MAX_TRAIN_SAMPLES="${MAX_TRAIN_SAMPLES:--1}"
MAX_EVAL_SAMPLES="${MAX_EVAL_SAMPLES:-}"
EVAL_STEPS="${EVAL_STEPS:-}"
SAVE_STEPS="${SAVE_STEPS:-}"
GRAD_ACCUM="${GRAD_ACCUM:-4}"
LORA_R="${LORA_R:-32}"
LORA_ALPHA="${LORA_ALPHA:-64}"
MAX_LENGTH="${MAX_LENGTH:-}"
PER_DEVICE_TRAIN_BATCH_SIZE="${PER_DEVICE_TRAIN_BATCH_SIZE:-1}"
PER_DEVICE_EVAL_BATCH_SIZE="${PER_DEVICE_EVAL_BATCH_SIZE:-1}"
DATALOADER_NUM_WORKERS="${DATALOADER_NUM_WORKERS:-4}"
RESUME_FROM_CHECKPOINT="${RESUME_FROM_CHECKPOINT:-}"
LOCAL_FILES_ONLY="${LOCAL_FILES_ONLY:-1}"
MERGE_AFTER_TRAIN="${MERGE_AFTER_TRAIN:-1}"
INIT_ADAPTER_DIR="${INIT_ADAPTER_DIR:-}"

set_default_if_empty() {
  local var_name="$1"
  local default_value="$2"
  if [[ -z "${!var_name}" ]]; then
    printf -v "$var_name" '%s' "$default_value"
  fi
}

case "$STAGE" in
  warmup)
    TRAIN_FILE="$PROJECT_ROOT/prepared_data/dental31_vlm/noaug/train_detection.jsonl"
    set_default_if_empty NUM_EPOCHS "1.0"
    set_default_if_empty LEARNING_RATE "1e-4"
    ;;
  main)
    TRAIN_FILE="$PROJECT_ROOT/prepared_data/dental31_vlm/noaug/train_mixed.jsonl"
    set_default_if_empty NUM_EPOCHS "1.0"
    set_default_if_empty LEARNING_RATE "7e-5"
    ;;
  robust)
    TRAIN_FILE="$PROJECT_ROOT/prepared_data/dental31_vlm/all/train_mixed.jsonl"
    set_default_if_empty NUM_EPOCHS "0.5"
    set_default_if_empty LEARNING_RATE "5e-5"
    ;;
  rare)
    TRAIN_FILE="$PROJECT_ROOT/prepared_data/dental31_vlm/all/train_rare_focus.jsonl"
    VALID_FILE="$PROJECT_ROOT/prepared_data/dental31_vlm/all/valid_rare_focus.jsonl"
    set_default_if_empty NUM_EPOCHS "3.0"
    set_default_if_empty LEARNING_RATE "3e-5"
    set_default_if_empty EVAL_STEPS "100"
    set_default_if_empty SAVE_STEPS "100"
    set_default_if_empty MAX_EVAL_SAMPLES "-1"
    ;;
  pilot)
    TRAIN_FILE="$PROJECT_ROOT/prepared_data/dental31_vlm/noaug/train_mixed.jsonl"
    set_default_if_empty NUM_EPOCHS "0.2"
    set_default_if_empty LEARNING_RATE "7e-5"
    set_default_if_empty MAX_TRAIN_SAMPLES "512"
    set_default_if_empty MAX_EVAL_SAMPLES "64"
    set_default_if_empty EVAL_STEPS "50"
    set_default_if_empty SAVE_STEPS "50"
    ;;
  *)
    echo "Unsupported STAGE=$STAGE"
    echo "Use one of: warmup, main, robust, rare, pilot"
    exit 1
    ;;
esac

set_default_if_empty MAX_EVAL_SAMPLES "256"
set_default_if_empty EVAL_STEPS "500"
set_default_if_empty SAVE_STEPS "500"
set_default_if_empty MAX_LENGTH "4608"

if [[ "$MODEL_ID" == /* || "$MODEL_ID" == ./* || "$MODEL_ID" == ../* ]]; then
  if [[ ! -e "$MODEL_ID" ]]; then
    echo "MODEL_ID does not exist: $MODEL_ID"
    echo "This slim OralPath bundle does not ship the base model."
    echo "Please set MODEL_ID to your external Qwen2.5-VL-7B-Instruct path."
    exit 1
  fi
fi

mkdir -p "$LOG_DIR"

echo "Run name: $RUN_NAME"
echo "Stage: $STAGE"
echo "Train file: $TRAIN_FILE"
echo "Valid file: $VALID_FILE"
echo "Output dir: $OUTPUT_DIR"
echo "Log file: $LOG_FILE"
echo "GPUs: $CUDA_VISIBLE_DEVICES"

cd $PROJECT_ROOT

exec "$TORCHRUN_BIN" \
  --nproc_per_node="$NPROC_PER_NODE" \
  scripts/train_qwen25vl_dental31_lora.py \
  --model-id "$MODEL_ID" \
  --train-file "$TRAIN_FILE" \
  --valid-file "$VALID_FILE" \
  --output-dir "$OUTPUT_DIR" \
  --num-train-epochs "$NUM_EPOCHS" \
  --learning-rate "$LEARNING_RATE" \
  --max-length "$MAX_LENGTH" \
  --per-device-train-batch-size "$PER_DEVICE_TRAIN_BATCH_SIZE" \
  --per-device-eval-batch-size "$PER_DEVICE_EVAL_BATCH_SIZE" \
  --gradient-accumulation-steps "$GRAD_ACCUM" \
  --dataloader-num-workers "$DATALOADER_NUM_WORKERS" \
  --eval-steps "$EVAL_STEPS" \
  --save-steps "$SAVE_STEPS" \
  --max-eval-samples "$MAX_EVAL_SAMPLES" \
  --max-train-samples "$MAX_TRAIN_SAMPLES" \
  --lora-r "$LORA_R" \
  --lora-alpha "$LORA_ALPHA" \
  ${INIT_ADAPTER_DIR:+--init-adapter-dir "$INIT_ADAPTER_DIR"} \
  ${RESUME_FROM_CHECKPOINT:+--resume-from-checkpoint "$RESUME_FROM_CHECKPOINT"} \
  ${LOCAL_FILES_ONLY:+--local-files-only} \
  ${MERGE_AFTER_TRAIN:+--merge-after-train} \
  2>&1 | tee "$LOG_FILE"
