#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATASET_ROOT_DEFAULT = PROJECT_ROOT / "31类常见牙科疾病数据集"
OUTPUT_ROOT_DEFAULT = PROJECT_ROOT / "prepared_data" / "dental31_vlm"

CLASS_NAMES_EN = {
    0: "Caries",
    1: "Crown",
    2: "Filling",
    3: "Implant",
    4: "Malaligned",
    5: "Mandibular Canal",
    6: "Missing teeth",
    7: "Periapical lesion",
    8: "Retained root",
    9: "Root Canal Treatment",
    10: "Root Piece",
    11: "impacted tooth",
    12: "maxillary sinus",
    13: "Bone Loss",
    14: "Fracture teeth",
    15: "Permanent Teeth",
    16: "Supra Eruption",
    17: "TAD",
    18: "abutment",
    19: "attrition",
    20: "bone defect",
    21: "gingival former",
    22: "metal band",
    23: "orthodontic brackets",
    24: "permanent retainer",
    25: "post - core",
    26: "plating",
    27: "wire",
    28: "Cyst",
    29: "Root resorption",
    30: "Primary teeth",
}

CLASS_NAMES_ZH = {
    0: "龋坏",
    1: "牙冠",
    2: "充填体",
    3: "种植体",
    4: "错位牙",
    5: "下颌管",
    6: "缺失牙",
    7: "根尖周病变",
    8: "残根",
    9: "根管治疗",
    10: "牙根残片",
    11: "阻生牙",
    12: "上颌窦",
    13: "骨丧失",
    14: "折裂牙",
    15: "恒牙",
    16: "过萌",
    17: "种植支抗钉",
    18: "基台",
    19: "磨耗",
    20: "骨缺损",
    21: "牙龈成形器",
    22: "金属带环",
    23: "正畸托槽",
    24: "固定保持器",
    25: "桩核",
    26: "接骨板",
    27: "金属丝",
    28: "囊肿",
    29: "牙根吸收",
    30: "乳牙",
}

CLASS_GROUPS = {
    "病变与异常": {
        0, 4, 7, 11, 13, 14, 16, 19, 20, 28, 29
    },
    "修复与治疗": {
        1, 2, 3, 9, 17, 18, 21, 22, 23, 24, 25, 26, 27
    },
    "解剖与牙体结构": {
        5, 6, 8, 10, 12, 15, 30
    },
}


@dataclass
class Instance:
    class_id: int
    label_en: str
    label_zh: str
    source_format: str
    bbox: List[float]
    polygon: List[List[float]] | None


def round4(value: float) -> float:
    return round(float(value), 4)


def load_instances(label_path: Path) -> List[Instance]:
    instances: List[Instance] = []
    lines = label_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    for line in lines:
        parts = line.strip().split()
        if not parts:
            continue
        class_id = int(float(parts[0]))
        coords = [float(x) for x in parts[1:]]

        if len(parts) == 5:
            cx, cy, w, h = coords
            x1 = max(0.0, cx - w / 2)
            y1 = max(0.0, cy - h / 2)
            x2 = min(1.0, cx + w / 2)
            y2 = min(1.0, cy + h / 2)
            instances.append(
                Instance(
                    class_id=class_id,
                    label_en=CLASS_NAMES_EN[class_id],
                    label_zh=CLASS_NAMES_ZH[class_id],
                    source_format="bbox",
                    bbox=[round4(x1), round4(y1), round4(x2), round4(y2)],
                    polygon=None,
                )
            )
            continue

        if len(coords) >= 4 and len(coords) % 2 == 0:
            points = [[coords[i], coords[i + 1]] for i in range(0, len(coords), 2)]
            xs = [p[0] for p in points]
            ys = [p[1] for p in points]
            instances.append(
                Instance(
                    class_id=class_id,
                    label_en=CLASS_NAMES_EN[class_id],
                    label_zh=CLASS_NAMES_ZH[class_id],
                    source_format="polygon",
                    bbox=[
                        round4(max(0.0, min(xs))),
                        round4(max(0.0, min(ys))),
                        round4(min(1.0, max(xs))),
                        round4(min(1.0, max(ys))),
                    ],
                    polygon=[[round4(x), round4(y)] for x, y in points],
                )
            )
    instances.sort(key=lambda x: (x.class_id, x.bbox[1], x.bbox[0]))
    return instances


def format_detection_response(instances: List[Instance]) -> str:
    payload: List[Dict[str, Any]] = []
    per_class_index: Dict[int, int] = defaultdict(int)
    for item in instances:
        per_class_index[item.class_id] += 1
        payload.append(
            {
                "target_id": f"{item.label_en}-{per_class_index[item.class_id]}",
                "label_en": item.label_en,
                "label_zh": item.label_zh,
                "bbox_2d": item.bbox,
            }
        )
    return json.dumps(payload, ensure_ascii=False)


def format_summary_response(instances: List[Instance]) -> str:
    class_counter = Counter(item.class_id for item in instances)
    group_lines: List[str] = []
    for group_name, class_ids in CLASS_GROUPS.items():
        parts: List[str] = []
        for class_id, count in sorted(class_counter.items()):
            if class_id in class_ids:
                parts.append(f"{CLASS_NAMES_ZH[class_id]}（{CLASS_NAMES_EN[class_id]}）{count}处")
        if parts:
            group_lines.append(f"- {group_name}：{'；'.join(parts)}。")
    total_targets = sum(class_counter.values())
    total_classes = len(class_counter)
    lines = [
        "主要发现：",
        f"- 当前标注共包含 {total_targets} 个目标，涉及 {total_classes} 类口腔影像对象。",
    ]
    lines.extend(group_lines)
    lines.append("- 本摘要仅依据数据集已有标注生成，不代表完整临床诊断。")
    return "\n".join(lines)


def format_rare_focus_response(instances: List[Instance], rare_class_ids: set[int]) -> str:
    payload: List[Dict[str, Any]] = []
    per_class_index: Dict[int, int] = defaultdict(int)
    for item in instances:
        if item.class_id not in rare_class_ids:
            continue
        per_class_index[item.class_id] += 1
        payload.append(
            {
                "target_id": f"{item.label_en}-{per_class_index[item.class_id]}",
                "label_en": item.label_en,
                "label_zh": item.label_zh,
                "bbox_2d": item.bbox,
            }
        )
    return json.dumps(payload, ensure_ascii=False)


def make_detection_example(example_id: str, image_path: str, instances: List[Instance]) -> Dict[str, Any]:
    prompt = (
        "<image>\n"
        "请识别这张牙科影像中所有已标注目标，并仅返回 JSON 数组。"
        "每个元素必须包含 label_en、label_zh 和 bbox_2d。"
        "bbox_2d 使用归一化坐标 [x1, y1, x2, y2]。不要输出额外解释。"
    )
    return {
        "id": example_id,
        "task": "detection_json",
        "images": [image_path],
        "messages": [
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": format_detection_response(instances)},
        ],
    }


def make_summary_example(example_id: str, image_path: str, instances: List[Instance]) -> Dict[str, Any]:
    prompt = (
        "<image>\n"
        "请根据图中已有标注，用中文总结这张牙科影像的主要发现。"
        "请按“病变与异常”“修复与治疗”“解剖与牙体结构”三个方面组织输出，"
        "不要编造未标注信息。"
    )
    return {
        "id": example_id,
        "task": "summary_zh",
        "images": [image_path],
        "messages": [
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": format_summary_response(instances)},
        ],
    }


def make_rare_focus_example(
    example_id: str,
    image_path: str,
    instances: List[Instance],
    rare_class_ids: set[int],
) -> Dict[str, Any] | None:
    rare_present = [item for item in instances if item.class_id in rare_class_ids]
    if not rare_present:
        return None
    labels = sorted({f"{item.label_zh}（{item.label_en}）" for item in rare_present})
    prompt = (
        "<image>\n"
        "请只关注这张牙科影像中的长尾类别目标，"
        f"候选类别为：{', '.join(labels)}。"
        "仅返回包含这些目标的 JSON 数组，每个元素包含 label_en、label_zh 和 bbox_2d。"
    )
    return {
        "id": example_id,
        "task": "rare_focus_json",
        "images": [image_path],
        "messages": [
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": format_rare_focus_response(instances, rare_class_ids)},
        ],
    }


def write_jsonl(path: Path, rows: Iterable[Dict[str, Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            count += 1
    return count


def write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def build_examples_for_split(
    dataset_root: Path,
    split: str,
    include_augmented: bool,
    rare_class_ids: set[int],
) -> Dict[str, List[Dict[str, Any]]]:
    split_root = dataset_root / split
    img_dir = split_root / "images"
    lbl_dir = split_root / "labels"
    detection_rows: List[Dict[str, Any]] = []
    summary_rows: List[Dict[str, Any]] = []
    rare_rows: List[Dict[str, Any]] = []

    for image_path in sorted(img_dir.iterdir()):
        if not image_path.is_file():
            continue
        if image_path.suffix.lower() not in {".jpg", ".jpeg", ".png", ".bmp", ".webp"}:
            continue
        if (not include_augmented) and "_aug_" in image_path.stem:
            continue
        label_path = lbl_dir / f"{image_path.stem}.txt"
        if not label_path.exists():
            continue
        instances = load_instances(label_path)
        if not instances:
            continue

        repo_relative_image = str(image_path).replace(
            "/home/linux/OralPath-oral-medicine-multiagent-reasoning-agents/",
            "",
        )
        base_id = f"{split}-{image_path.stem}"
        detection_rows.append(
            make_detection_example(f"{base_id}-det", repo_relative_image, instances)
        )
        summary_rows.append(
            make_summary_example(f"{base_id}-sum", repo_relative_image, instances)
        )
        rare_row = make_rare_focus_example(
            f"{base_id}-rare",
            repo_relative_image,
            instances,
            rare_class_ids,
        )
        if rare_row is not None:
            rare_rows.append(rare_row)

    mixed_rows = detection_rows + summary_rows + rare_rows
    return {
        "detection": detection_rows,
        "summary": summary_rows,
        "rare_focus": rare_rows,
        "mixed": mixed_rows,
    }


def collect_global_counts(dataset_root: Path) -> Counter:
    counts = Counter()
    for split in ["train", "valid", "test"]:
        for label_path in (dataset_root / split / "labels").glob("*.txt"):
            for item in load_instances(label_path):
                counts[item.class_id] += 1
    return counts


def build_metadata(dataset_root: Path, rare_class_ids: set[int], global_counts: Counter) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "dataset_root": str(dataset_root),
        "num_classes": len(CLASS_NAMES_EN),
        "rare_class_threshold_strategy": "global instance count <= 500",
        "rare_classes": [
            {
                "class_id": class_id,
                "label_en": CLASS_NAMES_EN[class_id],
                "label_zh": CLASS_NAMES_ZH[class_id],
                "instances": global_counts[class_id],
            }
            for class_id in sorted(rare_class_ids)
        ],
        "class_distribution": [
            {
                "class_id": class_id,
                "label_en": CLASS_NAMES_EN[class_id],
                "label_zh": CLASS_NAMES_ZH[class_id],
                "instances": global_counts[class_id],
            }
            for class_id, _ in global_counts.most_common()
        ],
    }
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Convert Dental31 YOLO labels into VLM instruction data.")
    parser.add_argument("--dataset-root", type=Path, default=DATASET_ROOT_DEFAULT)
    parser.add_argument("--output-root", type=Path, default=OUTPUT_ROOT_DEFAULT)
    parser.add_argument("--rare-threshold", type=int, default=500)
    args = parser.parse_args()

    dataset_root = args.dataset_root
    output_root = args.output_root
    output_root.mkdir(parents=True, exist_ok=True)

    global_counts = collect_global_counts(dataset_root)
    rare_class_ids = {class_id for class_id, count in global_counts.items() if count <= args.rare_threshold}

    metadata = build_metadata(dataset_root, rare_class_ids, global_counts)
    write_json(output_root / "metadata.json", metadata)

    manifest: Dict[str, Any] = {}
    for mode_name, include_augmented in [("all", True), ("noaug", False)]:
        mode_manifest: Dict[str, Any] = {}
        for split in ["train", "valid", "test"]:
            split_examples = build_examples_for_split(
                dataset_root=dataset_root,
                split=split,
                include_augmented=include_augmented if split == "train" else True,
                rare_class_ids=rare_class_ids,
            )
            for task_name, rows in split_examples.items():
                out_path = output_root / mode_name / f"{split}_{task_name}.jsonl"
                num_rows = write_jsonl(out_path, rows)
                mode_manifest[f"{split}_{task_name}"] = {
                    "path": str(out_path),
                    "num_rows": num_rows,
                }
        manifest[mode_name] = mode_manifest

    write_json(output_root / "manifest.json", manifest)
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
