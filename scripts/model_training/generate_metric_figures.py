#!/usr/bin/env python3

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont


PROJECT_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = PROJECT_ROOT / "assets" / "model_metric_figures"


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def try_font(size: int) -> ImageFont.ImageFont:
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    ]
    for candidate in candidates:
        p = Path(candidate)
        if p.exists():
            return ImageFont.truetype(str(p), size=size)
    return ImageFont.load_default()


FONT_TITLE = try_font(24)
FONT_AXIS = try_font(16)
FONT_LABEL = try_font(14)
FONT_TICK = try_font(12)


def make_canvas(width: int = 1200, height: int = 720) -> tuple[Image.Image, ImageDraw.ImageDraw]:
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    return image, draw


def draw_line_chart(
    title: str,
    x_values: list[float],
    y_values: list[float],
    x_label: str,
    y_label: str,
    output_path: Path,
    line_color: str = "#2E6FBB",
) -> None:
    image, draw = make_canvas()
    width, height = image.size
    left, top, right, bottom = 120, 80, width - 60, height - 100

    draw.text((left, 30), title, fill="black", font=FONT_TITLE)
    draw.line((left, bottom, right, bottom), fill="black", width=2)
    draw.line((left, bottom, left, top), fill="black", width=2)

    if not x_values or not y_values:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        image.save(output_path)
        return

    x_min, x_max = min(x_values), max(x_values)
    y_min, y_max = min(y_values), max(y_values)
    if x_max == x_min:
        x_max += 1
    if y_max == y_min:
        y_max += 1e-6

    def map_x(value: float) -> float:
        return left + (value - x_min) / (x_max - x_min) * (right - left)

    def map_y(value: float) -> float:
        return bottom - (value - y_min) / (y_max - y_min) * (bottom - top)

    pts = [(map_x(x), map_y(y)) for x, y in zip(x_values, y_values)]
    for idx in range(1, len(pts)):
        draw.line((pts[idx - 1], pts[idx]), fill=line_color, width=3)

    for x_tick in [x_min, x_max]:
        x_px = map_x(x_tick)
        draw.line((x_px, bottom, x_px, bottom + 6), fill="black", width=1)
        draw.text((x_px - 10, bottom + 10), f"{x_tick:.0f}", fill="black", font=FONT_TICK)
    for y_tick in [y_min, y_max]:
        y_px = map_y(y_tick)
        draw.line((left - 6, y_px, left, y_px), fill="black", width=1)
        draw.text((25, y_px - 8), f"{y_tick:.3f}", fill="black", font=FONT_TICK)

    draw.text((width // 2 - 30, height - 50), x_label, fill="black", font=FONT_AXIS)
    draw.text((10, top - 10), y_label, fill="black", font=FONT_AXIS)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(output_path)


def draw_bar_chart(
    title: str,
    labels: list[str],
    values: list[float],
    output_path: Path,
    y_max: float | None = None,
    bar_color: str = "#4C956C",
) -> None:
    image, draw = make_canvas()
    width, height = image.size
    left, top, right, bottom = 120, 80, width - 60, height - 120

    draw.text((left, 30), title, fill="black", font=FONT_TITLE)
    draw.line((left, bottom, right, bottom), fill="black", width=2)
    draw.line((left, bottom, left, top), fill="black", width=2)

    if not values:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        image.save(output_path)
        return

    value_max = y_max if y_max is not None else max(values) * 1.15
    value_max = max(value_max, 1e-6)

    bar_count = len(values)
    usable_w = right - left
    slot = usable_w / bar_count
    bar_w = slot * 0.55

    for idx, (label, value) in enumerate(zip(labels, values)):
        x1 = left + idx * slot + (slot - bar_w) / 2
        x2 = x1 + bar_w
        bar_h = (value / value_max) * (bottom - top)
        y1 = bottom - bar_h
        draw.rectangle((x1, y1, x2, bottom), fill=bar_color, outline="black")
        draw.text((x1, y1 - 20), f"{value:.3f}", fill="black", font=FONT_TICK)
        draw.text((x1 - 10, bottom + 10), label, fill="black", font=FONT_LABEL)

    for y_tick in [0.0, value_max]:
        y_px = bottom if y_tick == 0 else top
        draw.line((left - 6, y_px, left, y_px), fill="black", width=1)
        draw.text((25, y_px - 8), f"{y_tick:.2f}", fill="black", font=FONT_TICK)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(output_path)


def draw_grouped_bar_chart(
    title: str,
    labels: list[str],
    series_a_name: str,
    series_a: list[float],
    series_b_name: str,
    series_b: list[float],
    output_path: Path,
    y_max: float | None = None,
) -> None:
    image, draw = make_canvas()
    width, height = image.size
    left, top, right, bottom = 120, 80, width - 60, height - 120

    draw.text((left, 30), title, fill="black", font=FONT_TITLE)
    draw.line((left, bottom, right, bottom), fill="black", width=2)
    draw.line((left, bottom, left, top), fill="black", width=2)

    value_max = y_max if y_max is not None else max(series_a + series_b) * 1.2
    value_max = max(value_max, 1e-6)

    bar_count = len(labels)
    usable_w = right - left
    slot = usable_w / bar_count
    bar_w = slot * 0.28

    for idx, label in enumerate(labels):
        base_x = left + idx * slot + slot * 0.2
        for name, values, color, x_shift in [
            (series_a_name, series_a, "#2E6FBB", 0),
            (series_b_name, series_b, "#C86432", bar_w + 6),
        ]:
            value = values[idx]
            x1 = base_x + x_shift
            x2 = x1 + bar_w
            bar_h = (value / value_max) * (bottom - top)
            y1 = bottom - bar_h
            draw.rectangle((x1, y1, x2, bottom), fill=color, outline="black")
            draw.text((x1 - 2, y1 - 18), f"{value:.3f}", fill="black", font=FONT_TICK)
        draw.text((base_x - 8, bottom + 10), label, fill="black", font=FONT_LABEL)

    draw.rectangle((right - 220, top + 10, right - 10, top + 60), outline="black")
    draw.rectangle((right - 205, top + 20, right - 185, top + 40), fill="#2E6FBB", outline="black")
    draw.text((right - 175, top + 20), series_a_name, fill="black", font=FONT_LABEL)
    draw.rectangle((right - 205, top + 45, right - 185, top + 65), fill="#C86432", outline="black")
    draw.text((right - 175, top + 45), series_b_name, fill="black", font=FONT_LABEL)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(output_path)


def main() -> int:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    main_state = load_json(PROJECT_ROOT / "outputs" / "qwen25vl_dental31_main_20260328_034011" / "trainer_state.json")
    rare_state = load_json(PROJECT_ROOT / "outputs" / "qwen25vl_dental31_rare_ep2" / "trainer_state.json")
    structured_metrics = load_json(PROJECT_ROOT / "reports" / "qwen25vl_dental31_structured_eval_quick256_random" / "structured_metrics.json")
    baseline_metrics = load_json(Path("/tmp/qwen25vl_baseline_structured_eval_64_random/structured_metrics.json"))
    finetuned_64_metrics = load_json(Path("/tmp/qwen25vl_finetuned_structured_eval_64_random/structured_metrics.json"))

    def extract_train_curve(state: dict[str, Any]) -> tuple[list[float], list[float], list[float]]:
        steps, losses, lrs = [], [], []
        for item in state.get("log_history", []):
            if "loss" in item and "step" in item:
                steps.append(float(item["step"]))
                losses.append(float(item["loss"]))
                lrs.append(float(item.get("learning_rate", 0.0)))
        return steps, losses, lrs

    main_steps, main_losses, main_lrs = extract_train_curve(main_state)
    rare_steps, rare_losses, rare_lrs = extract_train_curve(rare_state)

    draw_line_chart(
        title="Main Stage Training Loss Curve",
        x_values=main_steps,
        y_values=main_losses,
        x_label="Step",
        y_label="Loss",
        output_path=OUTPUT_DIR / "main_training_loss_curve.png",
    )
    draw_line_chart(
        title="Main Stage Learning Rate Curve",
        x_values=main_steps,
        y_values=main_lrs,
        x_label="Step",
        y_label="Learning Rate",
        output_path=OUTPUT_DIR / "main_learning_rate_curve.png",
        line_color="#C86432",
    )
    draw_line_chart(
        title="Rare Stage Training Loss Curve",
        x_values=rare_steps,
        y_values=rare_losses,
        x_label="Step",
        y_label="Loss",
        output_path=OUTPUT_DIR / "rare_training_loss_curve.png",
        line_color="#4C956C",
    )

    struct_labels = [
        "Parse",
        "LabelF1",
        "DetF1",
        "Prec",
        "Recall",
        "IoU",
        "SummaryF1",
        "RougeL",
    ]
    struct_values = [
        structured_metrics["structured"]["json_parse_rate"],
        structured_metrics["structured"]["label_f1_mean"],
        structured_metrics["structured"]["det_f1_iou50"],
        structured_metrics["structured"]["det_precision_iou50"],
        structured_metrics["structured"]["det_recall_iou50"],
        structured_metrics["structured"]["matched_iou_mean"],
        structured_metrics["summary"]["token_f1_mean"],
        structured_metrics["summary"]["rouge_l_f1_mean"],
    ]
    draw_bar_chart(
        title="Structured Evaluation Metrics (Fine-tuned Model)",
        labels=struct_labels,
        values=struct_values,
        output_path=OUTPUT_DIR / "structured_eval_metrics_bar.png",
        y_max=1.0,
    )

    compare_labels = ["Parse", "LabelF1", "DetF1", "SummaryF1", "RougeL"]
    baseline_values = [
        baseline_metrics["structured"]["json_parse_rate"],
        baseline_metrics["structured"]["label_f1_mean"],
        baseline_metrics["structured"]["det_f1_iou50"],
        baseline_metrics["summary"]["token_f1_mean"],
        baseline_metrics["summary"]["rouge_l_f1_mean"],
    ]
    tuned_values = [
        finetuned_64_metrics["structured"]["json_parse_rate"],
        finetuned_64_metrics["structured"]["label_f1_mean"],
        finetuned_64_metrics["structured"]["det_f1_iou50"],
        finetuned_64_metrics["summary"]["token_f1_mean"],
        finetuned_64_metrics["summary"]["rouge_l_f1_mean"],
    ]
    draw_grouped_bar_chart(
        title="Base Model vs Fine-tuned Model (Structured Eval)",
        labels=compare_labels,
        series_a_name="Base",
        series_a=baseline_values,
        series_b_name="Fine-tuned",
        series_b=tuned_values,
        output_path=OUTPUT_DIR / "baseline_vs_finetuned_bar.png",
        y_max=1.0,
    )

    summary = {
        "generated_files": [
            str(OUTPUT_DIR / "main_training_loss_curve.png"),
            str(OUTPUT_DIR / "main_learning_rate_curve.png"),
            str(OUTPUT_DIR / "rare_training_loss_curve.png"),
            str(OUTPUT_DIR / "structured_eval_metrics_bar.png"),
            str(OUTPUT_DIR / "baseline_vs_finetuned_bar.png"),
        ]
    }
    (OUTPUT_DIR / "figure_manifest.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
