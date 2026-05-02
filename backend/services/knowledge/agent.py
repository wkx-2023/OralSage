from __future__ import annotations

import re
from dataclasses import replace
from typing import Any

from backend.services.retrieval import EvidenceChunk, explain_relevance, search_knowledge_base


SOURCE_LABELS = {
    "guideline": "指南/共识",
    "clinical_norm": "指南/共识",
    "textbook": "教材",
    "clinical_case": "病例",
    "general_reference": "通用参考",
}

TOPICS: list[dict[str, Any]] = [
    {
        "id": "caries",
        "name": "龋病",
        "group": "常见问题",
        "keywords": ["龋病", "龋齿", "牙体龋坏", "窝沟龋", "邻面龋"],
    },
    {
        "id": "pulpitis",
        "name": "牙髓炎",
        "group": "常见问题",
        "keywords": ["牙髓炎", "急性牙髓炎", "慢性牙髓炎", "自发痛", "夜间痛", "冷热痛"],
    },
    {
        "id": "apical_periodontitis",
        "name": "根尖周炎",
        "group": "常见问题",
        "keywords": ["根尖周炎", "根尖病变", "根尖脓肿", "咬合痛", "根管治疗"],
    },
    {
        "id": "periodontitis",
        "name": "牙周炎",
        "group": "常见问题",
        "keywords": ["牙周炎", "牙周袋", "牙槽骨吸收", "松动", "龈下刮治"],
    },
    {
        "id": "impacted_wisdom_tooth",
        "name": "阻生齿 / 智齿",
        "group": "常见问题",
        "keywords": ["阻生齿", "阻生牙", "第三磨牙", "智齿", "下牙槽神经", "CBCT"],
    },
    {
        "id": "pericoronitis",
        "name": "冠周炎",
        "group": "常见问题",
        "keywords": ["冠周炎", "智齿冠周炎", "冠周脓肿", "盲袋", "局部冲洗"],
    },
    {
        "id": "oral_mucosa",
        "name": "口腔黏膜病",
        "group": "专科方向",
        "keywords": ["口腔黏膜病", "白斑", "扁平苔藓", "复发性阿弗他溃疡", "癌前病变"],
    },
    {
        "id": "implant",
        "name": "种植相关",
        "group": "专科方向",
        "keywords": ["口腔种植", "种植体", "种植体周炎", "上颌窦提升", "骨增量"],
    },
    {
        "id": "prosthodontics",
        "name": "修复相关",
        "group": "专科方向",
        "keywords": ["口腔修复", "牙列缺损", "全瓷冠", "义齿", "咬合调整"],
    },
    {
        "id": "pediatric",
        "name": "儿童口腔",
        "group": "专科方向",
        "keywords": ["儿童口腔", "乳牙", "年轻恒牙", "牙髓切断术", "根尖诱导"],
    },
    {
        "id": "orthodontics",
        "name": "正畸相关",
        "group": "专科方向",
        "keywords": ["口腔正畸", "错颌畸形", "矫治", "数字化检查", "保持器"],
    },
]

DIMENSIONS: list[dict[str, Any]] = [
    {
        "id": "definition",
        "name": "疾病概念",
        "query_terms": ["定义", "概念", "分类"],
        "content_types": ["definition", "general"],
    },
    {
        "id": "etiology",
        "name": "病因/机制",
        "query_terms": ["病因", "机制", "发病机制", "危险因素"],
        "content_types": ["etiology", "pathogenesis", "pathology"],
    },
    {
        "id": "clinical_feature",
        "name": "临床表现",
        "query_terms": ["临床表现", "症状", "体征", "疼痛特点"],
        "content_types": ["clinical_feature"],
    },
    {
        "id": "diagnosis",
        "name": "诊断要点",
        "query_terms": ["诊断", "诊断标准", "检查结果", "影像表现"],
        "content_types": ["diagnosis"],
    },
    {
        "id": "differential",
        "name": "鉴别诊断",
        "query_terms": ["鉴别诊断", "区别", "相似疾病"],
        "content_types": ["differential", "diagnosis"],
    },
    {
        "id": "exam",
        "name": "检查建议",
        "query_terms": ["检查建议", "叩诊", "探诊", "牙髓活力", "X线", "CBCT"],
        "content_types": ["diagnosis"],
    },
    {
        "id": "treatment",
        "name": "治疗原则",
        "query_terms": ["治疗原则", "处理", "适应证", "手术", "治疗"],
        "content_types": ["treatment"],
    },
    {
        "id": "risk",
        "name": "风险/禁忌证",
        "query_terms": ["风险", "禁忌证", "并发症", "安全核查"],
        "content_types": ["risk", "contraindication", "prognosis"],
    },
    {
        "id": "education",
        "name": "随访与患者教育",
        "query_terms": ["随访", "复诊", "患者教育", "口腔卫生", "注意事项"],
        "content_types": ["prognosis", "treatment"],
    },
]


def _compact(text: str, limit: int = 240) -> str:
    value = re.sub(r"\s+", " ", text or "").strip()
    value = re.sub(r"本资料仅用于学习交流使用[，,、\s]*禁止用于商业用途", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    if len(value) <= limit:
        return value
    return value[: limit - 3].rstrip() + "..."


def _clean_title(value: str | None) -> str:
    title = str(value or "").strip()
    title = re.sub(r"[（(]已压缩[）)]", "", title)
    title = title.replace("(OCR).pdf", "").replace("（OCR）.pdf", "")
    title = title.replace("(OCR)", "").replace("（OCR）", "").replace(".pdf", "")
    title = re.sub(r"\s+", " ", title).strip(" -｜|")
    return title


def _chunk_content_types(chunk: EvidenceChunk) -> set[str]:
    raw = (chunk.metadata or {}).get("content_type") or (chunk.metadata or {}).get("content_types") or []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return set()
    return {str(item) for item in raw}


def _topic_by_id(topic_id: str) -> dict[str, Any]:
    for topic in TOPICS:
        if topic["id"] == topic_id:
            return topic
    return TOPICS[0]


def _dimension_by_id(dimension_id: str) -> dict[str, Any] | None:
    if not dimension_id or dimension_id == "all":
        return None
    for dimension in DIMENSIONS:
        if dimension["id"] == dimension_id:
            return dimension
    return None


def _query_for(topic: dict[str, Any], dimension: dict[str, Any]) -> str:
    pieces = list(topic.get("keywords") or []) + list(dimension.get("query_terms") or [])
    return " ".join(dict.fromkeys(str(item) for item in pieces if item))


def _citation(chunk: EvidenceChunk, query: str, index: int) -> dict[str, Any]:
    metadata = dict(chunk.metadata or {})
    source = _clean_title(chunk.source)
    title = _clean_title(chunk.title) or source or "未命名依据"
    return {
        "id": chunk.id,
        "marker": f"依据{index}",
        "title": title,
        "source": source,
        "source_type": chunk.source_type,
        "source_label": SOURCE_LABELS.get(chunk.source_type, chunk.source_type),
        "page": metadata.get("page"),
        "section": metadata.get("section") or metadata.get("chapter") or "",
        "excerpt": _compact(chunk.text, 420),
        "score": round(float(chunk.score or 0.0), 4),
        "relevance": explain_relevance(query, chunk),
        "path": chunk.path,
        "metadata": metadata,
    }


def _knowledge_point(
    chunk: EvidenceChunk,
    topic: dict[str, Any],
    dimension: dict[str, Any],
    query: str,
    point_index: int,
) -> dict[str, Any]:
    citation = _citation(chunk, query, point_index)
    source_label = citation["source_label"]
    page = f" 第{citation['page']}页" if citation.get("page") else ""
    title = citation["section"] or citation["title"]
    if not title or title == citation["source"]:
        title = f"{topic['name']}：{dimension['name']}知识点"
    return {
        "id": f"{dimension['id']}:{chunk.id}",
        "topic_id": topic["id"],
        "dimension_id": dimension["id"],
        "title": title,
        "summary": _compact(chunk.text, 210),
        "source_badge": f"{source_label}{page}",
        "source_type": chunk.source_type,
        "source_label": source_label,
        "score": citation["score"],
        "relevance": citation["relevance"],
        "citations": [citation],
    }


def get_knowledge_topics() -> dict[str, Any]:
    groups: dict[str, list[dict[str, str]]] = {}
    for topic in TOPICS:
        groups.setdefault(topic["group"], []).append(
            {
                "id": topic["id"],
                "name": topic["name"],
                "group": topic["group"],
            }
        )
    return {
        "ok": True,
        "module": "knowledge_topics",
        "topics": TOPICS,
        "topic_groups": groups,
        "dimensions": DIMENSIONS,
        "source_types": [
            {"id": "all", "name": "全部来源"},
            {"id": "guideline", "name": "指南/共识"},
            {"id": "textbook", "name": "教材"},
            {"id": "clinical_case", "name": "病例"},
            {"id": "general_reference", "name": "通用参考"},
        ],
    }


def run_knowledge_view(
    topic_id: str,
    dimension_id: str = "all",
    source_type: str = "all",
    top_k: int = 4,
) -> dict[str, Any]:
    topic = _topic_by_id(topic_id)
    selected_dimension = _dimension_by_id(dimension_id)
    dimensions = [selected_dimension] if selected_dimension else DIMENSIONS
    source_types = None if source_type in ("", "all", None) else {source_type}
    per_dimension = max(1, min(int(top_k or 4), 8))

    groups: list[dict[str, Any]] = []
    all_point_count = 0
    seen_global: set[str] = set()

    for dimension in dimensions:
        query = _query_for(topic, dimension)
        recall = max(per_dimension * 4, 12)
        chunks = [replace(chunk, metadata=dict(chunk.metadata or {})) for chunk in search_knowledge_base(query, top_k=recall, source_types=source_types)]
        wanted_types = set(dimension.get("content_types") or [])
        preferred = [chunk for chunk in chunks if not wanted_types or _chunk_content_types(chunk).intersection(wanted_types)]
        candidates = preferred + [chunk for chunk in chunks if chunk.id not in {item.id for item in preferred}]

        points = []
        for chunk in candidates:
            if chunk.id in seen_global:
                continue
            points.append(_knowledge_point(chunk, topic, dimension, query, len(points) + 1))
            seen_global.add(chunk.id)
            if len(points) >= per_dimension:
                break

        all_point_count += len(points)
        groups.append(
            {
                "id": dimension["id"],
                "name": dimension["name"],
                "query": query,
                "count": len(points),
                "points": points,
            }
        )

    return {
        "ok": True,
        "module": "knowledge_view",
        "topic": {"id": topic["id"], "name": topic["name"], "group": topic["group"], "keywords": topic["keywords"]},
        "dimension": selected_dimension["id"] if selected_dimension else "all",
        "source_type": source_type,
        "count": all_point_count,
        "groups": groups,
    }
