from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


DEFAULT_CONFIG = PROJECT_ROOT / "configs" / "knowledge_graph.yaml"


def stable_id(prefix: str, *parts: object, length: int = 16) -> str:
    raw = "||".join(str(part) for part in parts)
    digest = hashlib.sha1(raw.encode("utf-8", errors="ignore")).hexdigest()[:length]
    return f"{prefix}:{digest}"


def slugify(value: str) -> str:
    value = re.sub(r"\s+", "_", value.strip().lower())
    value = re.sub(r"[^\w\u4e00-\u9fff.-]+", "_", value)
    return value.strip("_") or "item"


def compact_text(text: str, limit: int = 420) -> str:
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    value = re.sub(r"本资料仅用于学习交流使用[，,、\s]*禁止用于商业用途", "", value)
    value = re.sub(r"\s+", " ", value).strip()
    if len(value) <= limit:
        return value
    return value[: limit - 3].rstrip() + "..."


def clean_label(value: str) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    text = text.replace("(OCR).pdf", "").replace("(OCR)", "")
    text = text.replace(".pdf", "")
    text = re.sub(r"（已压缩）|\\(已压缩\\)|已压缩", "", text)
    return text.strip(" -_")


def load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def iter_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                yield {"__bad_json__": True, "__path__": str(path), "__line__": line_no}
                continue
            if isinstance(item, dict):
                yield item


@dataclass
class Node:
    id: str
    type: str
    label: str
    properties: dict[str, Any] = field(default_factory=dict)


@dataclass
class Edge:
    id: str
    source: str
    target: str
    type: str
    weight: float = 1.0
    properties: dict[str, Any] = field(default_factory=dict)


class GraphBuilder:
    def __init__(self, max_support_ids_per_edge: int = 24) -> None:
        self.nodes: dict[str, Node] = {}
        self.edges: dict[tuple[str, str, str], Edge] = {}
        self.node_edge_counts: Counter[str] = Counter()
        self.max_support_ids_per_edge = max_support_ids_per_edge

    def add_node(self, node_id: str, node_type: str, label: str, **properties: Any) -> Node:
        label = clean_label(label) or node_id
        if node_id in self.nodes:
            node = self.nodes[node_id]
            node.label = node.label or label
            node.properties = merge_properties(node.properties, properties)
            return node
        node = Node(id=node_id, type=node_type, label=label, properties=drop_empty(properties))
        self.nodes[node_id] = node
        return node

    def add_edge(
        self,
        source: str,
        target: str,
        edge_type: str,
        *,
        weight: float = 1.0,
        evidence_id: str | None = None,
        **properties: Any,
    ) -> Edge:
        key = (source, target, edge_type)
        if key in self.edges:
            edge = self.edges[key]
            edge.weight = round(float(edge.weight) + float(weight), 6)
            edge.properties = merge_properties(edge.properties, properties)
        else:
            edge_id = stable_id("edge", source, target, edge_type)
            edge = Edge(
                id=edge_id,
                source=source,
                target=target,
                type=edge_type,
                weight=round(float(weight), 6),
                properties=drop_empty(properties),
            )
            self.edges[key] = edge
        if evidence_id:
            support_ids = edge.properties.setdefault("support_ids", [])
            if isinstance(support_ids, list) and evidence_id not in support_ids and len(support_ids) < self.max_support_ids_per_edge:
                support_ids.append(evidence_id)
            edge.properties["support_count"] = int(edge.properties.get("support_count") or 0) + 1
        self.node_edge_counts[source] += 1
        self.node_edge_counts[target] += 1
        return edge


def drop_empty(values: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in values.items() if value not in (None, "", [], {})}


def merge_properties(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    merged = dict(old)
    for key, value in drop_empty(new).items():
        if key not in merged or merged[key] in (None, "", [], {}):
            merged[key] = value
            continue
        current = merged[key]
        if isinstance(current, list):
            if isinstance(value, list):
                for item in value:
                    if item not in current:
                        current.append(item)
            elif value not in current:
                current.append(value)
        elif current != value:
            merged[key] = [current, value] if not isinstance(value, list) else [current, *value]
    return merged


CONTENT_TYPE_ALIASES = {
    "definition": "definition",
    "etiology": "etiology",
    "pathogenesis": "pathogenesis",
    "pathology": "pathology",
    "clinical_feature": "clinical_feature",
    "diagnosis": "diagnosis",
    "differential": "differential",
    "exam": "exam",
    "treatment": "treatment",
    "risk": "risk",
    "contraindication": "contraindication",
    "prognosis": "prognosis",
    "education": "education",
    "general": "general",
}


DIMENSION_TRIGGERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("definition", ("定义", "概念", "是指", "称为", "又称")),
    ("etiology", ("病因", "诱因", "危险因素", "相关因素", "原因")),
    ("pathogenesis", ("发病机制", "机制", "病理机制", "形成机制")),
    ("pathology", ("病理", "组织病理", "镜下", "病理改变", "病理表现")),
    ("clinical_feature", ("临床表现", "症状", "体征", "主诉", "疼痛", "肿胀", "松动")),
    ("diagnosis", ("诊断", "诊断标准", "确诊", "判断", "诊断依据")),
    ("differential", ("鉴别", "鉴别诊断", "区别", "需与", "相鉴别")),
    ("exam", ("检查", "影像", "X线", "CBCT", "叩诊", "探诊", "活检", "牙髓活力")),
    ("treatment", ("治疗", "处理", "手术", "用药", "根管", "拔除", "修复", "种植", "正畸")),
    ("risk", ("风险", "并发症", "危险", "癌变", "损伤", "感染")),
    ("contraindication", ("禁忌", "禁忌证", "不宜", "慎用")),
    ("prognosis", ("预后", "复发", "转归", "随访")),
    ("education", ("宣教", "口腔卫生", "复诊", "注意事项", "维护")),
)


COURSE_MODULES: list[dict[str, Any]] = [
    {
        "id": "oral_anatomy_physiology",
        "label": "口腔解剖与生理基础",
        "keywords": ["牙体解剖", "牙列", "咬合", "颌骨", "颞下颌", "唾液", "口腔生理", "神经", "血管"],
    },
    {
        "id": "oral_histology_pathology",
        "label": "口腔组织病理学",
        "keywords": ["组织病理", "病理", "上皮", "结缔组织", "囊肿", "肿瘤", "瘤样病变", "癌变", "异型增生"],
    },
    {
        "id": "cariology_endodontics",
        "label": "牙体牙髓病学",
        "keywords": ["龋", "龋齿", "龋病", "牙髓", "牙髓炎", "根尖", "根尖周", "根管", "牙体缺损", "充填"],
    },
    {
        "id": "periodontology",
        "label": "牙周病学",
        "keywords": ["牙周", "牙周炎", "牙龈", "牙周袋", "附着丧失", "牙槽骨吸收", "洁治", "刮治"],
    },
    {
        "id": "oral_mucosal_diseases",
        "label": "口腔黏膜病学",
        "keywords": ["黏膜", "白斑", "扁平苔藓", "阿弗他", "溃疡", "疱疹", "念珠菌", "红斑"],
    },
    {
        "id": "oral_maxillofacial_surgery",
        "label": "口腔颌面外科学",
        "keywords": ["拔除", "阻生", "智齿", "颌面", "颌骨", "面瘫", "创伤", "感染", "间隙感染", "上颌窦", "下牙槽神经"],
    },
    {
        "id": "prosthodontics",
        "label": "口腔修复学",
        "keywords": ["修复", "牙体缺损", "牙列缺损", "牙列缺失", "义齿", "全口义齿", "冠", "桥", "贴面"],
    },
    {
        "id": "orthodontics",
        "label": "口腔正畸学",
        "keywords": ["正畸", "错颌", "矫治", "矫治器", "牙移动", "保持器", "拥挤", "深覆合"],
    },
    {
        "id": "pediatric_dentistry",
        "label": "儿童口腔医学",
        "keywords": ["儿童", "乳牙", "恒牙", "年轻恒牙", "窝沟封闭", "氟化物", "牙外伤", "间隙保持"],
    },
    {
        "id": "implantology",
        "label": "口腔种植学",
        "keywords": ["种植", "种植体", "骨增量", "骨结合", "种植体周围炎", "上颌窦提升"],
    },
    {
        "id": "oral_radiology_diagnostics",
        "label": "口腔影像与诊断",
        "keywords": ["影像", "X线", "CBCT", "全景片", "曲面断层", "根尖片", "检查", "诊断"],
    },
    {
        "id": "preventive_public_oral_health",
        "label": "口腔预防与公共卫生",
        "keywords": ["预防", "口腔卫生", "菌斑", "刷牙", "氟", "窝沟封闭", "健康教育", "随访"],
    },
    {
        "id": "clinical_case_reasoning",
        "label": "临床病例与诊疗推理",
        "keywords": ["主诉", "现病史", "既往史", "门诊", "诊疗", "医嘱", "病例", "处置"],
    },
]


ONTOLOGY_TERMS: list[dict[str, Any]] = [
    {"label": "龋病", "type": "Disease", "aliases": ["龋", "龋齿", "牙体龋坏", "深龋"], "module": "cariology_endodontics"},
    {"label": "牙髓炎", "type": "Disease", "aliases": ["急性牙髓炎", "慢性牙髓炎", "不可复性牙髓炎"], "module": "cariology_endodontics"},
    {"label": "根尖周炎", "type": "Disease", "aliases": ["根尖周病", "根尖病变", "根尖周脓肿"], "module": "cariology_endodontics"},
    {"label": "牙体缺损", "type": "Finding", "aliases": ["牙体组织缺损"], "module": "prosthodontics"},
    {"label": "根管治疗", "type": "Treatment", "aliases": ["根管", "根管预备", "根管充填"], "module": "cariology_endodontics"},
    {"label": "牙周炎", "type": "Disease", "aliases": ["慢性牙周炎", "侵袭性牙周炎"], "module": "periodontology"},
    {"label": "牙龈炎", "type": "Disease", "aliases": ["牙龈出血", "牙龈红肿"], "module": "periodontology"},
    {"label": "牙周袋", "type": "Finding", "aliases": ["深牙周袋"], "module": "periodontology"},
    {"label": "牙槽骨吸收", "type": "Finding", "aliases": ["牙槽骨", "骨吸收"], "module": "periodontology"},
    {"label": "牙齿松动", "type": "SymptomOrSign", "aliases": ["松动", "牙松动"], "module": "periodontology"},
    {"label": "口腔白斑", "type": "Disease", "aliases": ["白斑", "口腔白斑病"], "module": "oral_mucosal_diseases"},
    {"label": "口腔扁平苔藓", "type": "Disease", "aliases": ["扁平苔藓"], "module": "oral_mucosal_diseases"},
    {"label": "复发性阿弗他溃疡", "type": "Disease", "aliases": ["阿弗他", "口腔溃疡", "复发性口腔溃疡"], "module": "oral_mucosal_diseases"},
    {"label": "口腔黏膜病", "type": "DiseaseGroup", "aliases": ["黏膜", "口腔黏膜"], "module": "oral_mucosal_diseases"},
    {"label": "口腔念珠菌病", "type": "Disease", "aliases": ["念珠菌", "鹅口疮"], "module": "oral_mucosal_diseases"},
    {"label": "阻生牙", "type": "Disease", "aliases": ["阻生", "阻生齿", "阻生智齿", "智齿", "第三磨牙"], "module": "oral_maxillofacial_surgery"},
    {"label": "冠周炎", "type": "Disease", "aliases": ["智齿冠周炎", "冠周脓肿"], "module": "oral_maxillofacial_surgery"},
    {"label": "拔牙术", "type": "Treatment", "aliases": ["拔除", "拔牙", "阻生牙拔除"], "module": "oral_maxillofacial_surgery"},
    {"label": "下牙槽神经损伤", "type": "Complication", "aliases": ["下牙槽神经", "神经损伤"], "module": "oral_maxillofacial_surgery"},
    {"label": "上颌窦相关风险", "type": "Complication", "aliases": ["上颌窦", "上颌窦穿孔"], "module": "oral_maxillofacial_surgery"},
    {"label": "口腔颌面部囊肿", "type": "DiseaseGroup", "aliases": ["囊肿", "颌骨囊肿"], "module": "oral_histology_pathology"},
    {"label": "口腔颌面部肿瘤", "type": "DiseaseGroup", "aliases": ["肿瘤", "瘤样病变"], "module": "oral_histology_pathology"},
    {"label": "组织病理检查", "type": "Examination", "aliases": ["组织病理", "病理检查", "镜下"], "module": "oral_histology_pathology"},
    {"label": "活检", "type": "Examination", "aliases": ["活组织检查"], "module": "oral_histology_pathology"},
    {"label": "口腔修复", "type": "Treatment", "aliases": ["修复", "冠修复", "固定修复", "可摘局部义齿", "全口义齿"], "module": "prosthodontics"},
    {"label": "牙列缺损", "type": "Finding", "aliases": ["缺牙", "牙列缺失"], "module": "prosthodontics"},
    {"label": "种植修复", "type": "Treatment", "aliases": ["种植", "种植体", "种植义齿"], "module": "implantology"},
    {"label": "种植体周围炎", "type": "Disease", "aliases": ["种植体周围病"], "module": "implantology"},
    {"label": "口腔正畸治疗", "type": "Treatment", "aliases": ["正畸", "矫治", "矫治器"], "module": "orthodontics"},
    {"label": "错颌畸形", "type": "Disease", "aliases": ["错颌", "错𬌗", "牙列拥挤"], "module": "orthodontics"},
    {"label": "儿童口腔疾病", "type": "DiseaseGroup", "aliases": ["儿童", "儿童口腔"], "module": "pediatric_dentistry"},
    {"label": "乳牙疾病", "type": "DiseaseGroup", "aliases": ["乳牙"], "module": "pediatric_dentistry"},
    {"label": "年轻恒牙疾病", "type": "DiseaseGroup", "aliases": ["年轻恒牙", "恒牙"], "module": "pediatric_dentistry"},
    {"label": "牙外伤", "type": "Disease", "aliases": ["外伤牙", "牙折", "脱位"], "module": "pediatric_dentistry"},
    {"label": "疼痛", "type": "SymptomOrSign", "aliases": ["牙痛", "自发痛", "夜间痛", "冷热痛", "叩痛"], "module": "cariology_endodontics"},
    {"label": "肿胀", "type": "SymptomOrSign", "aliases": ["肿痛", "脓肿"], "module": "oral_maxillofacial_surgery"},
    {"label": "CBCT检查", "type": "Examination", "aliases": ["CBCT", "锥形束CT"], "module": "oral_radiology_diagnostics"},
    {"label": "X线检查", "type": "Examination", "aliases": ["X线", "全景片", "曲面断层", "根尖片"], "module": "oral_radiology_diagnostics"},
    {"label": "叩诊", "type": "Examination", "aliases": ["叩痛"], "module": "oral_radiology_diagnostics"},
    {"label": "探诊", "type": "Examination", "aliases": ["牙周探诊", "探诊深度"], "module": "periodontology"},
    {"label": "菌斑控制", "type": "Prevention", "aliases": ["菌斑", "口腔卫生", "刷牙"], "module": "preventive_public_oral_health"},
    {"label": "窝沟封闭", "type": "Prevention", "aliases": ["窝沟封闭剂"], "module": "preventive_public_oral_health"},
    {"label": "氟化物防龋", "type": "Prevention", "aliases": ["氟", "氟化物", "涂氟"], "module": "preventive_public_oral_health"},
    {"label": "颞下颌关节疾病", "type": "DiseaseGroup", "aliases": ["颞下颌", "颞下颌关节", "关节紊乱"], "module": "oral_anatomy_physiology"},
]


RELATION_RULES: list[dict[str, Any]] = [
    {
        "type": "has_clinical_feature",
        "from_types": {"Disease", "DiseaseGroup", "Finding"},
        "to_types": {"SymptomOrSign", "Finding"},
        "dimensions": {"clinical_feature"},
        "weight": 1.6,
    },
    {
        "type": "diagnosed_by",
        "from_types": {"Disease", "DiseaseGroup", "Finding"},
        "to_types": {"Examination"},
        "dimensions": {"diagnosis", "exam"},
        "weight": 1.7,
    },
    {
        "type": "treated_by",
        "from_types": {"Disease", "DiseaseGroup", "Finding"},
        "to_types": {"Treatment", "Prevention"},
        "dimensions": {"treatment", "education"},
        "weight": 1.8,
    },
    {
        "type": "has_risk",
        "from_types": {"Disease", "DiseaseGroup", "Treatment"},
        "to_types": {"Complication", "Finding"},
        "dimensions": {"risk", "contraindication", "prognosis"},
        "weight": 1.5,
    },
    {
        "type": "requires_pathology_evidence",
        "from_types": {"Disease", "DiseaseGroup", "Finding"},
        "to_types": {"Examination"},
        "dimensions": {"pathology", "diagnosis"},
        "target_labels": {"组织病理检查", "活检"},
        "weight": 1.7,
    },
]


def term_node_id(label: str) -> str:
    return "concept:" + slugify(label)


def build_term_index() -> tuple[dict[str, dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    by_label: dict[str, dict[str, Any]] = {}
    alias_index: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for term in ONTOLOGY_TERMS:
        canonical = clean_label(term["label"])
        item = {
            **term,
            "label": canonical,
            "id": term_node_id(canonical),
            "aliases": list(dict.fromkeys([canonical, *(term.get("aliases") or [])])),
        }
        by_label[canonical] = item
        for alias in item["aliases"]:
            alias_value = clean_label(alias)
            if alias_value:
                alias_index[alias_value.lower()].append(item)
    return by_label, alias_index


def find_terms(text: str, alias_index: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    haystack = str(text or "").lower()
    found: dict[str, dict[str, Any]] = {}
    for alias, terms in alias_index.items():
        if alias and alias in haystack:
            for term in terms:
                found[term["id"]] = term
    return sorted(found.values(), key=lambda item: (item.get("module", ""), item["label"]))


def infer_dimensions(record: dict[str, Any], text: str) -> list[str]:
    raw = record.get("content_type") or (record.get("metadata") if isinstance(record.get("metadata"), dict) else {}).get("content_type") or []
    if isinstance(raw, str):
        raw = [raw]
    dimensions = [CONTENT_TYPE_ALIASES[item] for item in raw if item in CONTENT_TYPE_ALIASES]
    haystack = f"{record.get('title') or ''}\n{record.get('section') or ''}\n{text}"
    for dim, triggers in DIMENSION_TRIGGERS:
        if any(trigger.lower() in haystack.lower() for trigger in triggers):
            dimensions.append(dim)
    return list(dict.fromkeys(dimensions or ["general"]))


def infer_modules(record: dict[str, Any], text: str, terms: list[dict[str, Any]]) -> list[str]:
    scores: Counter[str] = Counter()
    haystack = f"{record.get('source') or ''}\n{record.get('title') or ''}\n{record.get('chapter') or ''}\n{record.get('section') or ''}\n{text}"
    for term in terms:
        if term.get("module"):
            scores[term["module"]] += 3
    for module in COURSE_MODULES:
        for keyword in module["keywords"]:
            count = haystack.count(keyword)
            if count:
                scores[module["id"]] += min(count, 4)
    if str(record.get("source_type")) == "clinical_case":
        scores["clinical_case_reasoning"] += 2
    if not scores:
        return ["clinical_case_reasoning"] if str(record.get("source_type")) == "clinical_case" else ["oral_anatomy_physiology"]
    top = scores.most_common(3)
    max_score = top[0][1]
    return [module_id for module_id, score in top if score >= max(2, math.ceil(max_score * 0.45))]


def source_node_id(source: str, source_type: str) -> str:
    return stable_id("source", source_type, source)


def fragment_node_id(record: dict[str, Any], source_name: str) -> str:
    return "fragment:" + slugify(str(record.get("id") or stable_id("record", source_name, record.get("title"), record.get("text", "")[:80])))


def section_node_id(source_id: str, chapter: str, section: str) -> str:
    label = clean_label(chapter) or clean_label(section) or "未分章节"
    return stable_id("section", source_id, label)


def dimension_node_id(dimension: str) -> str:
    return "dimension:" + slugify(dimension)


def module_node_id(module_id: str) -> str:
    return "module:" + module_id


def topic_node_id(topic_label: str) -> str:
    return "topic:" + slugify(topic_label)


SECTION_PREFIX_RE = re.compile(
    r"^\s*(?:第[一二三四五六七八九十百0-9]+[章节篇]|[0-9]+(?:\.[0-9]+)*|[一二三四五六七八九十]+[、.．])\s*"
)


TOPIC_STOPWORDS = {
    "免责声明",
    "参考文献",
    "经验与提示",
    "中英文名词对照索引",
    "目录",
    "前言",
    "绪论",
    "附录",
    "本章小结",
    "复习思考题",
    "© Springer Science+Business Media",
}


def normalize_topic_label(*parts: str) -> str:
    for part in parts:
        value = clean_label(part)
        if not value:
            continue
        value = SECTION_PREFIX_RE.sub("", value)
        value = re.sub(r"^[A-Z]\s+[0-9]{1,3}\s*", "", value)
        value = re.sub(r"\s+", "", value)
        value = value.strip(" ：:，,。；;、-—")
        if not (2 <= len(value) <= 28):
            continue
        if value in TOPIC_STOPWORDS:
            continue
        if re.fullmatch(r"[A-Z]?\s*[0-9.\-—]+", value):
            continue
        if value.startswith("T/CHSA") or value.startswith("ICS") or value.startswith("CCS"):
            continue
        if "参考文献" in value or "免责声明" in value:
            continue
        return value
    return ""


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
            count += 1
    return count


def write_csv(path: Path, fieldnames: list[str], rows: Iterable[dict[str, Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
            count += 1
    return count


def build_graph(config_path: Path = DEFAULT_CONFIG, limit: int | None = None) -> dict[str, Any]:
    config = load_yaml(config_path)
    kg_config = config.get("knowledge_graph") or {}
    input_dir = PROJECT_ROOT / str(kg_config.get("input_dir") or "data/processed/knowledge_base")
    output_dir = PROJECT_ROOT / str(kg_config.get("output_dir") or "data/processed/knowledge_graph")
    excerpt_limit = int(kg_config.get("max_excerpt_chars") or 420)
    max_support = int(kg_config.get("max_support_ids_per_edge") or 24)
    overview_cfg = kg_config.get("overview") or {}
    max_concepts_per_module = int(overview_cfg.get("max_concepts_per_module") or 48)
    max_topics_per_module = int(overview_cfg.get("max_topics_per_module") or 24)
    max_cooccurrence_edges = int(overview_cfg.get("max_cooccurrence_edges") or 420)

    graph = GraphBuilder(max_support_ids_per_edge=max_support)
    _, alias_index = build_term_index()
    issues: list[dict[str, Any]] = []
    source_type_labels = config.get("source_types") or {}
    dimension_labels = {item["id"]: item["label"] for item in config.get("content_dimensions") or []}
    module_lookup = {module["id"]: module for module in COURSE_MODULES}

    course = config.get("course") or {}
    course_id = "course:" + slugify(str(course.get("id") or "oral_medicine_course"))
    graph.add_node(
        course_id,
        "Course",
        str(course.get("label") or "口腔医学课程"),
        description=course.get("description"),
        graph_version=(kg_config.get("version") or "0.1.0"),
    )

    for module in COURSE_MODULES:
        node_id = module_node_id(module["id"])
        graph.add_node(node_id, "CourseModule", module["label"], keywords=module["keywords"])
        graph.add_edge(course_id, node_id, "has_module", weight=1.0)

    for item in config.get("content_dimensions") or []:
        node_id = dimension_node_id(item["id"])
        graph.add_node(node_id, "LearningDimension", item["label"], dimension_id=item["id"])
        graph.add_edge(course_id, node_id, "has_learning_dimension", weight=1.0)

    term_by_id: dict[str, dict[str, Any]] = {}
    for term in ONTOLOGY_TERMS:
        label = clean_label(term["label"])
        node_id = term_node_id(label)
        term_by_id[node_id] = {**term, "label": label, "id": node_id}
        graph.add_node(
            node_id,
            str(term.get("type") or "Concept"),
            label,
            aliases=term.get("aliases") or [],
            canonical_label=label,
            module_id=term.get("module"),
            extraction="ontology_seed",
        )
        if term.get("module"):
            graph.add_edge(module_node_id(term["module"]), node_id, "teaches_concept", weight=2.0)

    source_counts: Counter[str] = Counter()
    module_counts: Counter[str] = Counter()
    dimension_counts: Counter[str] = Counter()
    concept_counts: Counter[str] = Counter()
    concept_module_counts: dict[str, Counter[str]] = defaultdict(Counter)
    concept_dimension_counts: dict[str, Counter[str]] = defaultdict(Counter)
    module_concept_counts: dict[str, Counter[str]] = defaultdict(Counter)
    topic_counts: Counter[str] = Counter()
    topic_module_counts: dict[str, Counter[str]] = defaultdict(Counter)
    topic_dimension_counts: dict[str, Counter[str]] = defaultdict(Counter)
    module_topic_counts: dict[str, Counter[str]] = defaultdict(Counter)
    module_dimension_counts: dict[str, Counter[str]] = defaultdict(Counter)
    source_fragment_counts: Counter[str] = Counter()
    cooccurrence: Counter[tuple[str, str]] = Counter()
    fragment_count = 0
    total_chars = 0

    input_files = sorted(input_dir.glob("*.jsonl"))
    for path in input_files:
        for record in iter_jsonl(path):
            if record.get("__bad_json__"):
                issues.append({"type": "bad_json", "path": record.get("__path__"), "line": record.get("__line__")})
                continue
            if limit is not None and fragment_count >= limit:
                break

            text = str(record.get("text") or "")
            if not text.strip():
                issues.append({"type": "empty_text", "path": str(path), "id": record.get("id")})
                continue

            source_name = clean_label(str(record.get("source") or path.stem))
            source_type = str(record.get("source_type") or "unknown")
            title = clean_label(str(record.get("title") or record.get("section") or source_name))
            chapter = clean_label(str(record.get("chapter") or (record.get("metadata") or {}).get("chapter") or ""))
            section = clean_label(str(record.get("section") or (record.get("metadata") or {}).get("section") or ""))
            page = record.get("page") or (record.get("metadata") if isinstance(record.get("metadata"), dict) else {}).get("page")
            quality_score = record.get("quality_score") or (record.get("metadata") if isinstance(record.get("metadata"), dict) else {}).get("quality_score")

            terms = find_terms(f"{title}\n{chapter}\n{section}\n{text}", alias_index)
            dimensions = infer_dimensions(record, text)
            modules = infer_modules(record, text, terms)
            topic_label = normalize_topic_label(section, chapter, title)

            source_id = source_node_id(source_name, source_type)
            source_label = source_type_labels.get(source_type, source_type)
            graph.add_node(
                source_id,
                "SourceDocument",
                source_name,
                source_type=source_type,
                source_label=source_label,
                path=record.get("path"),
            )
            source_counts[source_type] += 1
            source_fragment_counts[source_id] += 1

            sec_id = section_node_id(source_id, chapter, section)
            graph.add_node(
                sec_id,
                "SourceSection",
                chapter or section or title,
                chapter=chapter,
                section=section,
                source_id=source_id,
            )
            graph.add_edge(source_id, sec_id, "has_section", weight=1.0)

            frag_id = fragment_node_id(record, source_name)
            fragment_count += 1
            total_chars += len(text)
            graph.add_node(
                frag_id,
                "KnowledgeFragment",
                title,
                source_id=source_id,
                source=source_name,
                source_type=source_type,
                page=page,
                chapter=chapter,
                section=section,
                path=record.get("path"),
                excerpt=compact_text(text, excerpt_limit),
                char_count=len(text),
                quality_score=quality_score,
                dimensions=dimensions,
                module_ids=modules,
            )
            graph.add_edge(sec_id, frag_id, "has_fragment", weight=1.0)
            graph.add_edge(source_id, frag_id, "contains_fragment", weight=0.5)

            for module_id in modules:
                module_counts[module_id] += 1
                module_dimension_counts[module_id].update(dimensions)
                graph.add_edge(module_node_id(module_id), frag_id, "has_learning_material", weight=0.25)

            for dimension in dimensions:
                dimension_counts[dimension] += 1
                dim_id = dimension_node_id(dimension)
                graph.add_node(dim_id, "LearningDimension", dimension_labels.get(dimension, dimension), dimension_id=dimension)
                graph.add_edge(frag_id, dim_id, "covers_dimension", weight=1.0)

            topic_id = ""
            if topic_label:
                topic_id = topic_node_id(topic_label)
                topic_counts[topic_id] += 1
                topic_module_counts[topic_id].update(modules)
                topic_dimension_counts[topic_id].update(dimensions)
                graph.add_node(
                    topic_id,
                    "CurriculumTopic",
                    topic_label,
                    extraction="section_title",
                )
                graph.add_edge(topic_id, frag_id, "supported_by", weight=0.85, evidence_id=frag_id)
                graph.add_edge(frag_id, topic_id, "mentions_topic", weight=0.85)
                for module_id in modules:
                    module_topic_counts[module_id][topic_id] += 1
                    graph.add_edge(module_node_id(module_id), topic_id, "teaches_topic", weight=0.55, evidence_id=frag_id)
                for dimension in dimensions:
                    graph.add_edge(topic_id, dimension_node_id(dimension), "covers_dimension", weight=0.35, evidence_id=frag_id)

            term_ids = [term["id"] for term in terms]
            for term in terms:
                concept_id = term["id"]
                concept_counts[concept_id] += 1
                concept_module_counts[concept_id].update(modules)
                concept_dimension_counts[concept_id].update(dimensions)
                for module_id in modules:
                    module_concept_counts[module_id][concept_id] += 1
                graph.add_edge(concept_id, frag_id, "supported_by", weight=1.0, evidence_id=frag_id)
                graph.add_edge(frag_id, concept_id, "mentions_concept", weight=1.0)
                if topic_id:
                    graph.add_edge(topic_id, concept_id, "contains_concept", weight=0.65, evidence_id=frag_id)
                if term.get("module"):
                    graph.add_edge(module_node_id(term["module"]), concept_id, "teaches_concept", weight=0.2, evidence_id=frag_id)

            for left_index, left in enumerate(term_ids):
                for right in term_ids[left_index + 1 :]:
                    if left == right:
                        continue
                    pair = tuple(sorted((left, right)))
                    cooccurrence[pair] += 1

            add_rule_edges(graph, terms, dimensions, frag_id)

        if limit is not None and fragment_count >= limit:
            break

    enrich_concepts(graph, term_by_id, concept_counts, concept_module_counts, concept_dimension_counts, module_lookup)
    enrich_topics(graph, topic_counts, topic_module_counts, topic_dimension_counts, module_lookup)
    add_cooccurrence_edges(graph, cooccurrence, max_cooccurrence_edges)
    add_prerequisite_edges(graph)

    output_dir.mkdir(parents=True, exist_ok=True)

    nodes = sorted(graph.nodes.values(), key=lambda node: (node.type, node.label, node.id))
    edges = sorted(graph.edges.values(), key=lambda edge: (edge.type, edge.source, edge.target))

    node_rows = [
        {"id": node.id, "type": node.type, "label": node.label, "properties": node.properties}
        for node in nodes
    ]
    edge_rows = [
        {
            "id": edge.id,
            "source": edge.source,
            "target": edge.target,
            "type": edge.type,
            "weight": round(edge.weight, 6),
            "properties": edge.properties,
        }
        for edge in edges
    ]
    write_jsonl(output_dir / "nodes.jsonl", node_rows)
    write_jsonl(output_dir / "edges.jsonl", edge_rows)
    write_jsonl(output_dir / "issues.jsonl", issues)
    write_csv(
        output_dir / "nodes.csv",
        ["id", "type", "label", "properties"],
        ({**row, "properties": json.dumps(row["properties"], ensure_ascii=False)} for row in node_rows),
    )
    write_csv(
        output_dir / "edges.csv",
        ["id", "source", "target", "type", "weight", "properties"],
        ({**row, "properties": json.dumps(row["properties"], ensure_ascii=False)} for row in edge_rows),
    )

    overview = build_overview(
        graph=graph,
        course_id=course_id,
        module_concept_counts=module_concept_counts,
        module_topic_counts=module_topic_counts,
        module_dimension_counts=module_dimension_counts,
        concept_counts=concept_counts,
        topic_counts=topic_counts,
        cooccurrence=cooccurrence,
        max_concepts_per_module=max_concepts_per_module,
        max_topics_per_module=max_topics_per_module,
    )
    (output_dir / "overview.json").write_text(json.dumps(overview, ensure_ascii=False, indent=2), encoding="utf-8")

    summary = {
        "ok": True,
        "name": kg_config.get("name") or "oral_course_graph",
        "version": kg_config.get("version") or "0.1.0",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "input_dir": str(input_dir.relative_to(PROJECT_ROOT)),
        "output_dir": str(output_dir.relative_to(PROJECT_ROOT)),
        "input_files": [str(path.relative_to(PROJECT_ROOT)) for path in input_files],
        "counts": {
            "nodes": len(nodes),
            "edges": len(edges),
            "fragments": fragment_count,
            "sources": sum(1 for node in nodes if node.type == "SourceDocument"),
            "sections": sum(1 for node in nodes if node.type == "SourceSection"),
            "curriculum_topics": sum(1 for node in nodes if node.type == "CurriculumTopic"),
            "concepts": sum(1 for node in nodes if node.type not in {"Course", "CourseModule", "LearningDimension", "SourceDocument", "SourceSection", "KnowledgeFragment"}),
            "total_chars": total_chars,
            "issues": len(issues),
        },
        "by_source_type": dict(source_counts),
        "by_module": dict(module_counts),
        "by_dimension": dict(dimension_counts),
        "top_concepts": [
            {"id": concept_id, "label": graph.nodes[concept_id].label, "count": count}
            for concept_id, count in concept_counts.most_common(40)
            if concept_id in graph.nodes
        ],
        "top_topics": [
            {"id": topic_id, "label": graph.nodes[topic_id].label, "count": count}
            for topic_id, count in topic_counts.most_common(40)
            if topic_id in graph.nodes
        ],
        "source_fragment_counts": [
            {"id": source_id, "label": graph.nodes[source_id].label, "count": count}
            for source_id, count in source_fragment_counts.most_common(40)
            if source_id in graph.nodes
        ],
    }
    (output_dir / "graph_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def add_rule_edges(graph: GraphBuilder, terms: list[dict[str, Any]], dimensions: list[str], fragment_id: str) -> None:
    if len(terms) < 2:
        return
    dim_set = set(dimensions)
    for rule in RELATION_RULES:
        if rule.get("dimensions") and not dim_set.intersection(rule["dimensions"]):
            continue
        from_terms = [term for term in terms if term.get("type") in rule["from_types"]]
        to_terms = [term for term in terms if term.get("type") in rule["to_types"]]
        target_labels = rule.get("target_labels")
        if target_labels:
            to_terms = [term for term in to_terms if term["label"] in target_labels]
        for left in from_terms:
            for right in to_terms:
                if left["id"] == right["id"]:
                    continue
                graph.add_edge(
                    left["id"],
                    right["id"],
                    rule["type"],
                    weight=float(rule.get("weight") or 1.0),
                    evidence_id=fragment_id,
                    extraction="rule",
                )


def enrich_concepts(
    graph: GraphBuilder,
    term_by_id: dict[str, dict[str, Any]],
    concept_counts: Counter[str],
    concept_module_counts: dict[str, Counter[str]],
    concept_dimension_counts: dict[str, Counter[str]],
    module_lookup: dict[str, dict[str, Any]],
) -> None:
    for concept_id, count in concept_counts.items():
        if concept_id not in graph.nodes:
            continue
        module_counts = concept_module_counts.get(concept_id, Counter())
        dimension_counts = concept_dimension_counts.get(concept_id, Counter())
        primary_module = module_counts.most_common(1)[0][0] if module_counts else term_by_id.get(concept_id, {}).get("module")
        node = graph.nodes[concept_id]
        node.properties = merge_properties(
            node.properties,
            {
                "support_count": int(count),
                "primary_module_id": primary_module,
                "primary_module_label": module_lookup.get(primary_module, {}).get("label"),
                "module_counts": dict(module_counts.most_common()),
                "dimension_counts": dict(dimension_counts.most_common()),
            },
        )


def enrich_topics(
    graph: GraphBuilder,
    topic_counts: Counter[str],
    topic_module_counts: dict[str, Counter[str]],
    topic_dimension_counts: dict[str, Counter[str]],
    module_lookup: dict[str, dict[str, Any]],
) -> None:
    for topic_id, count in topic_counts.items():
        if topic_id not in graph.nodes:
            continue
        module_counts = topic_module_counts.get(topic_id, Counter())
        dimension_counts = topic_dimension_counts.get(topic_id, Counter())
        primary_module = module_counts.most_common(1)[0][0] if module_counts else ""
        node = graph.nodes[topic_id]
        node.properties = merge_properties(
            node.properties,
            {
                "support_count": int(count),
                "primary_module_id": primary_module,
                "primary_module_label": module_lookup.get(primary_module, {}).get("label"),
                "module_counts": dict(module_counts.most_common()),
                "dimension_counts": dict(dimension_counts.most_common()),
            },
        )


def add_cooccurrence_edges(graph: GraphBuilder, cooccurrence: Counter[tuple[str, str]], max_edges: int) -> None:
    for (left, right), count in cooccurrence.most_common(max_edges):
        if count < 2:
            continue
        graph.add_edge(
            left,
            right,
            "co_occurs_with",
            weight=round(math.log1p(count), 6),
            cooccurrence_count=int(count),
            extraction="cooccurrence",
        )


def add_prerequisite_edges(graph: GraphBuilder) -> None:
    sequence = [
        ("concept:疼痛", "concept:牙髓炎"),
        ("concept:龋病", "concept:牙髓炎"),
        ("concept:牙髓炎", "concept:根尖周炎"),
        ("concept:牙周袋", "concept:牙周炎"),
        ("concept:牙槽骨吸收", "concept:牙周炎"),
        ("concept:X线检查", "concept:根尖周炎"),
        ("concept:CBCT检查", "concept:阻生牙"),
        ("concept:组织病理检查", "concept:口腔白斑"),
        ("concept:活检", "concept:口腔扁平苔藓"),
        ("concept:牙列缺损", "concept:口腔修复"),
        ("concept:菌斑控制", "concept:牙周炎"),
        ("concept:氟化物防龋", "concept:龋病"),
    ]
    normalized = [(term_node_id(a.replace("concept:", "")), term_node_id(b.replace("concept:", ""))) for a, b in sequence]
    for left, right in normalized:
        if left in graph.nodes and right in graph.nodes:
            graph.add_edge(left, right, "prerequisite_of", weight=1.0, extraction="curriculum_seed")


def build_overview(
    *,
    graph: GraphBuilder,
    course_id: str,
    module_concept_counts: dict[str, Counter[str]],
    module_topic_counts: dict[str, Counter[str]],
    module_dimension_counts: dict[str, Counter[str]],
    concept_counts: Counter[str],
    topic_counts: Counter[str],
    cooccurrence: Counter[tuple[str, str]],
    max_concepts_per_module: int,
    max_topics_per_module: int,
) -> dict[str, Any]:
    overview_nodes: dict[str, dict[str, Any]] = {}
    overview_edges: list[dict[str, Any]] = []

    def add_overview_node(node_id: str, level: str, **extra: Any) -> None:
        if node_id not in graph.nodes:
            return
        node = graph.nodes[node_id]
        overview_nodes[node_id] = {
            "id": node.id,
            "type": node.type,
            "label": node.label,
            "level": level,
            "properties": {**node.properties, **drop_empty(extra)},
        }

    add_overview_node(course_id, "course")
    for module in COURSE_MODULES:
        module_id = module_node_id(module["id"])
        add_overview_node(
            module_id,
            "module",
            fragment_count=sum(module_dimension_counts.get(module["id"], Counter()).values()),
            dimension_counts=dict(module_dimension_counts.get(module["id"], Counter()).most_common()),
        )
        overview_edges.append({"source": course_id, "target": module_id, "type": "has_module", "weight": 1.0})

    included_concepts: set[str] = set()
    for module in COURSE_MODULES:
        module_id = module_node_id(module["id"])
        topic_counter = module_topic_counts.get(module["id"], Counter())
        for topic_id, count in topic_counter.most_common(max_topics_per_module):
            if topic_id not in graph.nodes:
                continue
            add_overview_node(topic_id, "topic", support_count=int(topic_counts.get(topic_id, count)))
            overview_edges.append(
                {
                    "source": module_id,
                    "target": topic_id,
                    "type": "teaches_topic",
                    "weight": round(math.log1p(count), 6),
                    "support_count": int(count),
                }
            )

        counter = module_concept_counts.get(module["id"], Counter())
        for concept_id, count in counter.most_common(max_concepts_per_module):
            if concept_id not in graph.nodes:
                continue
            included_concepts.add(concept_id)
            add_overview_node(concept_id, "concept", support_count=int(concept_counts.get(concept_id, count)))
            overview_edges.append(
                {
                    "source": module_id,
                    "target": concept_id,
                    "type": "teaches_concept",
                    "weight": round(math.log1p(count), 6),
                    "support_count": int(count),
                }
            )

    for (left, right), count in cooccurrence.most_common(300):
        if count < 3:
            continue
        if left in included_concepts and right in included_concepts:
            overview_edges.append(
                {
                    "source": left,
                    "target": right,
                    "type": "co_occurs_with",
                    "weight": round(math.log1p(count), 6),
                    "cooccurrence_count": int(count),
                }
            )

    return {
        "nodes": list(overview_nodes.values()),
        "edges": overview_edges,
        "course_id": course_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a course-level oral medicine knowledge graph.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="Path to knowledge graph YAML config.")
    parser.add_argument("--limit", type=int, default=None, help="Optional fragment limit for quick debugging.")
    args = parser.parse_args()
    summary = build_graph(args.config, args.limit)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
