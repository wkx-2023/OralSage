# 31 类牙科数据集到 Qwen2.5-VL 训练计划

## 一、数据集现状

- 原始数据位置：`31类常见牙科疾病数据集/`
- 任务本质：牙科影像的检测/分割标注，不是现成的 VLM 问答数据
- 类别数：31
- 总图像数：23413
- `train`：18962
- `valid`：2871
- `test`：1580
- `train` 中增强图像比例约 50%
- 标注格式为混合型：
  - `bbox`：94792 个实例
  - `polygon`：136891 个实例

## 二、已生成的 VLM 训练材料

脚本：

- `scripts/prepare_dental31_vlm_dataset.py`

输出目录：

- `prepared_data/dental31_vlm/`

关键文件：

- `prepared_data/dental31_vlm/metadata.json`
- `prepared_data/dental31_vlm/manifest.json`
- `prepared_data/dental31_vlm/noaug/train_detection.jsonl`
- `prepared_data/dental31_vlm/noaug/train_summary.jsonl`
- `prepared_data/dental31_vlm/noaug/train_rare_focus.jsonl`
- `prepared_data/dental31_vlm/noaug/train_mixed.jsonl`
- `prepared_data/dental31_vlm/all/train_mixed.jsonl`

数据转换策略：

1. 将 `bbox` 和 `polygon` 统一转换为归一化 `bbox_2d`
2. 保留类别英文名和中文名
3. 生成三类 VLM 任务：
   - `detection_json`：结构化定位
   - `summary_zh`：中文口腔影像摘要
   - `rare_focus_json`：长尾类别补强

## 三、推荐训练材料

### 主实验

- 预热阶段：`prepared_data/dental31_vlm/noaug/train_detection.jsonl`
- 主训练阶段：`prepared_data/dental31_vlm/noaug/train_mixed.jsonl`
- 扩展阶段：`prepared_data/dental31_vlm/all/train_mixed.jsonl`
- 长尾补强阶段：`prepared_data/dental31_vlm/all/train_rare_focus.jsonl`

### 验证与测试

- 验证：`prepared_data/dental31_vlm/noaug/valid_mixed.jsonl`
- 测试：`prepared_data/dental31_vlm/noaug/test_mixed.jsonl`

说明：

- `noaug` 更适合作为主训练集，避免模型过度拟合 Roboflow 风格增强痕迹
- `all` 适合作为第二阶段扩展训练，用来提高鲁棒性
- `rare_focus` 用来补强长尾类别，不建议单独作为主训练集

## 四、推荐训练方法

### 目标模型

- 基座：`Qwen/Qwen2.5-VL-7B-Instruct`

### 推荐微调方式

- `QLoRA`
- 4-bit base model
- LoRA 目标模块：
  - `q_proj`
  - `k_proj`
  - `v_proj`
  - `o_proj`
  - `gate_proj`
  - `up_proj`
  - `down_proj`

### 推荐超参数

- `lora_r=32`
- `lora_alpha=64`
- `lora_dropout=0.05`
- `per_device_train_batch_size=1`
- `gradient_accumulation_steps=8`
- `global_batch_size=16`（双卡）
- `learning_rate`：
  - 预热阶段：`1e-4`
  - 主训练阶段：`7e-5`
  - 扩展阶段：`5e-5`
  - 长尾补强阶段：`3e-5`
- `lr_scheduler_type=cosine`
- `warmup_ratio=0.03`
- `max_seq_length=3072`
- `bf16=true`
- `gradient_checkpointing=true`

## 五、推荐分阶段训练方案

### Stage 1：定位预热

目标：

- 先让模型学会“图中有什么、在哪”

数据：

- `noaug/train_detection.jsonl`

训练量：

- `9443` 条样本

轮数：

- `1 epoch`

### Stage 2：主任务联合训练

目标：

- 同时学习结构化定位和中文口腔摘要表达

数据：

- `noaug/train_mixed.jsonl`

训练量：

- `19284` 条样本

轮数：

- `1 epoch`

### Stage 3：增强样本扩展

目标：

- 提高模型对尺度、对比度、旋转、裁剪扰动的鲁棒性

数据：

- `all/train_mixed.jsonl`

训练量：

- `38568` 条样本

轮数：

- `0.5 ~ 1 epoch`

### Stage 4：长尾类别补强

目标：

- 补强 `Cyst`、`Root resorption`、`TAD`、`bone defect` 等极少样本类

数据：

- `all/train_rare_focus.jsonl`

训练量：

- `796` 条样本

轮数：

- `2 ~ 4 epochs`

## 六、当前服务器资源与训练可行性

当前服务器观察结果：

- GPU：`2 x NVIDIA A100 40GB`
- 当前在线服务会占用显存：
  - `flare25-qwen25vl`
  - `hulumed-openai`
  - `nexent-dentalgpt`

如果不先停掉这些服务，双卡可用显存不足，不建议直接开训。

建议训练前执行：

```bash
sudo systemctl stop flare25-qwen25vl.service
sudo systemctl stop hulumed-openai.service
docker stop nexent-dentalgpt
```

## 七、训练时长估算

以下估算基于：

- 独占 `2 x A100 40GB`
- `Qwen2.5-VL-7B-Instruct`
- `QLoRA`
- 双卡 DDP
- 图像分辨率以当前数据主流尺寸为主
- `global_batch_size=16`

### 仅做实用主线训练

包含：

- Stage 1
- Stage 2
- Stage 4

预计训练时间：

- `4 ~ 7 小时`

### 完整训练方案

包含：

- Stage 1
- Stage 2
- Stage 3
- Stage 4

预计训练时间：

- `8 ~ 14 小时`

### 如果只能单卡训练

预计训练时间大约变为：

- 实用主线：`8 ~ 12 小时`
- 完整方案：`14 ~ 24 小时`

## 八、建议你实际怎么跑

如果你要尽快得到一个能用的口腔专用模型，建议按下面顺序：

1. 先停掉当前在线推理服务，释放双卡
2. 先跑 `Stage 1 + Stage 2`
3. 用 `valid_mixed` 看指标和生成样例
4. 如果效果已经明显提升，再跑 `Stage 3`
5. 最后补 `Stage 4` 处理长尾类别

如果你想优先追求最强效果，而不是最快完成，建议直接跑完整方案。
