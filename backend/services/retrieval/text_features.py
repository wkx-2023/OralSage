from __future__ import annotations

import re
from collections import Counter
from typing import Iterable


MEDICAL_HINTS = (
    "阻生", "阻生齿", "阻生牙", "阻生智齿", "第三磨牙", "智齿", "冠周炎", "智齿冠周炎",
    "拔除", "下牙槽神经", "上颌窦", "CBCT", "曲面断层", "全景片",
    "根管", "牙髓", "牙髓炎", "根尖", "根尖周炎", "冷刺激", "自发痛", "夜间痛",
    "牙周", "牙周炎", "牙周袋", "牙槽骨", "探诊", "叩诊", "龋", "龋齿",
    "松动", "肿胀", "疼痛", "种植", "修复", "黏膜", "白斑", "颞下颌", "正畸",
    "儿童", "乳牙", "恒牙", "囊肿", "牙源性囊肿", "口腔鳞癌", "扁平苔藓",
    "成釉细胞瘤", "活检", "组织病理", "异型增生", "癌变",
)

GENERIC_ANCHORS = {
    "口腔", "临床", "决策", "诊疗", "诊疗指南", "指南", "共识", "规范", "检查",
    "治疗", "治疗方案", "适应证", "禁忌证", "鉴别诊断", "建议", "处理",
}

CORE_HINTS = tuple(hint for hint in MEDICAL_HINTS if hint not in GENERIC_ANCHORS)

CONTENT_TYPE_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("definition", ("定义", "概念", "是指", "称为")),
    ("etiology", ("病因", "诱因", "危险因素", "相关因素")),
    ("pathogenesis", ("发病机制", "机制", "病理机制", "为什么", "形成机制")),
    ("pathology", ("病理变化", "组织学", "显微", "大体", "镜下", "病理表现")),
    ("clinical_feature", ("临床表现", "症状", "体征", "表现为", "主诉")),
    ("diagnosis", ("诊断", "诊断标准", "检查", "影像", "活检")),
    ("differential", ("鉴别", "鉴别诊断", "区别", "需与")),
    ("treatment", ("治疗", "处理", "管理", "手术", "用药", "方案")),
    ("prognosis", ("预后", "复发", "转归", "随访")),
    ("risk", ("风险", "并发症", "危险", "癌变")),
    ("contraindication", ("禁忌", "禁忌证", "不宜", "慎用")),
)


def tokenize(text: str) -> list[str]:
    text = (text or "").lower()
    latin = re.findall(r"[a-z0-9][a-z0-9+\-]{1,}", text)
    chinese = re.findall(r"[\u4e00-\u9fff]+", text)
    tokens: list[str] = []
    tokens.extend(latin)
    for segment in chinese:
        if len(segment) <= 2:
            tokens.append(segment)
            continue
        tokens.extend(segment[i : i + 2] for i in range(len(segment) - 1))
        tokens.extend(segment[i : i + 3] for i in range(len(segment) - 2))
    for hint in MEDICAL_HINTS:
        if hint.lower() in text:
            tokens.append(hint.lower())
    return [token for token in tokens if token.strip()]


def query_anchors(query: str) -> list[str]:
    query_lower = (query or "").lower()
    anchors: list[str] = []
    for hint in MEDICAL_HINTS:
        if hint.lower() in query_lower:
            anchors.append(hint.lower())
    anchors.extend(re.findall(r"[a-z0-9][a-z0-9+\-]{2,}", query_lower))
    anchors.extend(term for term in re.findall(r"[\u4e00-\u9fff]{2,}", query_lower) if len(term) <= 8)
    return list(dict.fromkeys(anchors))[:32]


def core_anchors(query: str) -> list[str]:
    query_lower = (query or "").lower()
    return list(dict.fromkeys(hint.lower() for hint in CORE_HINTS if hint.lower() in query_lower))


def infer_content_types(text: str, title: str = "") -> list[str]:
    haystack = f"{title}\n{text}".lower()
    labels: list[str] = []
    for label, triggers in CONTENT_TYPE_RULES:
        if any(trigger.lower() in haystack for trigger in triggers):
            labels.append(label)
    return labels or ["general"]


def extract_entities(text: str, title: str = "", limit: int = 12) -> list[str]:
    haystack = f"{title}\n{text}".lower()
    entities: list[str] = []
    for hint in sorted(MEDICAL_HINTS, key=len, reverse=True):
        if hint.lower() in haystack and hint not in entities:
            entities.append(hint)
        if len(entities) >= limit:
            break
    return entities


def term_counts(texts: Iterable[str]) -> Counter[str]:
    counts: Counter[str] = Counter()
    for text in texts:
        counts.update(tokenize(text))
    return counts
