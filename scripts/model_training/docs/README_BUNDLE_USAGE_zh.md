# OralPath 精简训练复现包使用说明

## 目录入口

精简训练复现包根目录：

- `/data/OralPath_export_20260328/07_training_repro_bundle_oralpath_only`

## 当前包内包含的关键内容

- 最终 OralPath LoRA：`models/OralPath/final_adapter/`
- OralPath 合并说明：`models/OralPath/MERGE_INSTRUCTIONS_zh.md`
- OralPath 合并脚本：`scripts/merge_oralpath_adapter.py`
- 处理后数据：`prepared_data/dental31_vlm/`
- 训练脚本：`scripts/train_qwen25vl_dental31_lora.py`
- 启动脚本：`scripts/run_qwen25vl_dental31_lora.sh`
- 评测结果：`results/`

## 当前包内未包含的模型内容

- 基座模型 `Qwen2.5-VL-7B-Instruct`
- OralPath 已合并整模型
- 中间阶段 LoRA 权重

因此，如果需要重新训练、继续评测或合并部署，请自行准备一份外部基座模型路径。

## 建议环境

建议使用当前服务器已有的 Python 环境：

- `/home/linux/.venvs/hulumed/bin/python`
- `/home/linux/.venvs/hulumed/bin/torchrun`

## dry-run 验证

```bash
cd /data/OralPath_export_20260328/07_training_repro_bundle_oralpath_only
/home/linux/.venvs/hulumed/bin/python scripts/train_qwen25vl_dental31_lora.py \
  --model-id /path/to/Qwen2.5-VL-7B-Instruct \
  --dry-run \
  --local-files-only
```

## 启动主训练

```bash
cd /data/OralPath_export_20260328/07_training_repro_bundle_oralpath_only
MODEL_ID=/path/to/Qwen2.5-VL-7B-Instruct CUDA_VISIBLE_DEVICES=0,1 bash scripts/run_qwen25vl_dental31_lora.sh
```

## 合并 OralPath 最终权重

```bash
cd /data/OralPath_export_20260328/07_training_repro_bundle_oralpath_only
/home/linux/.venvs/hulumed/bin/python scripts/merge_oralpath_adapter.py \
  --base-model /path/to/Qwen2.5-VL-7B-Instruct \
  --adapter-dir models/OralPath/final_adapter \
  --output-dir outputs/OralPath_merged
```

## 输出位置

- 默认训练输出：`runs/`
- 评测结果：`results/`
- 图表：`assets/model_metric_figures/`
- 合并整模型输出：自定义 `--output-dir`
