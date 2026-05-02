from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
import re
from typing import Any

from backend.services.llm_client import chat_completion, is_llm_configured
from backend.services.retrieval import EvidenceChunk, explain_relevance, rerank_chunks, reranker_status, search_knowledge_base


SOURCE_TYPE_LABELS = {
    "guideline": "指南/共识",
    "clinical_norm": "临床规范",
    "textbook": "教材",
    "general_reference": "通用医学参考",
    "clinical_case": "脱敏病例",
    "literature": "医学文献",
}

DISPLAY_NOISE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"[（(]\s*已压缩\s*[）)]"),
    re.compile(r"[｜|_\-\s]*高清\s*全彩\s*[（(]?\s*OCR\s*[）)]?", re.IGNORECASE),
    re.compile(r"[｜|_\-\s]*全彩\s*[（(]?\s*OCR\s*[）)]?", re.IGNORECASE),
    re.compile(r"[｜|_\-\s]*高?清?\s*[（(]\s*OCR\s*[）)]", re.IGNORECASE),
    re.compile(r"[（(]\s*OCR\s*[）)]", re.IGNORECASE),
    re.compile(r"\bOCR\b", re.IGNORECASE),
    re.compile(r"\.pdf\b", re.IGNORECASE),
    re.compile(r"本资料仅用于学习交流使用[，,、\s]*禁止用于商业用途"),
    re.compile(r"本资料仅用于学习交流使用"),
    re.compile(r"禁止用于商业用途"),
)

RAW_CASE_FIELD_PATTERN = re.compile(r"\b(?:Unnamed|nan|None)\s*[:：]?\s*\d*\s*[:：]?", re.IGNORECASE)
LONG_IDENTIFIER_PATTERN = re.compile(r"\b[A-Z]?\d{8,}\b", re.IGNORECASE)
DATE_TIME_PATTERN = re.compile(r"\b\d{4}[-/]\d{1,2}[-/]\d{1,2}(?:\s+\d{1,2}:\d{2}:\d{2})?\b")
DIAGNOSIS_CODE_PATTERN = re.compile(r"\b[A-Z]\d{2}(?:\.\d+)*\b", re.IGNORECASE)
PUBLIC_METADATA_KEYS = {
    "page",
    "chapter",
    "section",
    "content_type",
    "content_types",
    "entities",
    "quality_score",
    "parent_id",
    "retrieval_channels",
    "rerank_score",
    "rerank_source",
}


@dataclass
class EvidenceItem:
    marker: str
    source: str
    source_type: str
    title: str = ""
    excerpt: str = ""
    relevance: str = ""
    score: float | None = None
    path: str = ""
    year: str | None = None
    journal: str | None = None
    link: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "marker": self.marker,
            "source": clean_title(self.source),
            "source_type": self.source_type,
            "source_type_label": SOURCE_TYPE_LABELS.get(self.source_type, self.source_type),
            "title": clean_title(self.title),
            "excerpt": clean_display_text(self.excerpt),
            "relevance": self.relevance,
            "score": round(float(self.score), 4) if self.score is not None else None,
            "path": clean_display_text(self.path),
            "year": self.year,
            "journal": self.journal,
            "link": self.link,
            "metadata": public_metadata(self.metadata),
        }


def compact(text: str, limit: int = 420) -> str:
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(value) <= limit:
        return value
    return value[: limit - 3].rstrip() + "..."


def clean_display_text(text: str, limit: int | None = None) -> str:
    value = str(text or "")
    for pattern in DISPLAY_NOISE_PATTERNS:
        value = pattern.sub(" ", value)
    value = value.replace("｜", " ").replace("|", " ")
    value = re.sub(r"\s+", " ", value).strip(" -_·，,。")
    if limit:
        return compact(value, limit)
    return value


def clean_title(text: str) -> str:
    return clean_display_text(text).replace("  ", " ").strip()


def public_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    cleaned: dict[str, Any] = {}
    for key, value in dict(metadata or {}).items():
        if key not in PUBLIC_METADATA_KEYS:
            continue
        if isinstance(value, str):
            cleaned[key] = clean_display_text(value)
        elif isinstance(value, list):
            cleaned[key] = [clean_display_text(item) if isinstance(item, str) else item for item in value]
        elif isinstance(value, dict):
            cleaned[key] = {
                str(child_key): clean_display_text(child_value) if isinstance(child_value, str) else child_value
                for child_key, child_value in value.items()
                if child_key in PUBLIC_METADATA_KEYS
            }
        else:
            cleaned[key] = value
    return cleaned


def _case_values(text: str) -> list[str]:
    values: list[str] = []
    ignored_exact = {
        "未行手术和操作",
        "牙科检查",
        "口腔颌面外科门诊",
        "口腔综合科门诊",
        "口腔内科门诊",
        "口腔外科门诊",
        "口腔种植科门诊",
        "正畸科门诊",
        "无",
        "未诉",
    }
    ignored_contains = ("主任医师", "主治医师", "住院医师", "医师")

    for raw in re.split(r"[\r\n]+", str(text or "")):
        line = raw.strip()
        if not line:
            continue
        if "：" in line:
            value = line.split("：", 1)[1]
        elif ":" in line:
            value = line.split(":", 1)[1]
        else:
            value = line
        value = clean_display_text(value)
        value = DATE_TIME_PATTERN.sub(" ", value)
        value = DIAGNOSIS_CODE_PATTERN.sub(" ", value)
        value = LONG_IDENTIFIER_PATTERN.sub(" ", value)
        value = re.sub(r"\s+", " ", value).strip(" ，,。")
        if not value:
            continue
        if re.fullmatch(r"\d+(?:\.\d+)?", value):
            continue
        if value in ignored_exact:
            continue
        if any(term in value for term in ignored_contains):
            continue
        if value not in values:
            values.append(value)
    return values


def _heuristic_case_summary(text: str, query: str, limit: int = 420) -> str:
    values = _case_values(text)
    if not values:
        return "脱敏门诊记录提示存在与检索主题相关的就诊信息，但原始字段不足以稳定归纳具体病理结论。"

    terms = [
        term
        for term in re.findall(r"[\u4e00-\u9fff]{2,8}|[A-Za-z][A-Za-z0-9+\-]{2,}", query or "")
        if term
    ]
    clinical_keywords = (
        "主诉",
        "诉",
        "检查",
        "影像",
        "拍片",
        "CBCT",
        "CT",
        "病理",
        "囊肿",
        "阻生",
        "冠周",
        "牙根",
        "吸收",
        "神经",
        "肿",
        "痛",
        "不适",
        "诊断",
        "待排",
        "建议",
        "治疗",
        "手术",
        "拔除",
        "会诊",
    )
    selected = [
        value
        for value in values
        if any(term.lower() in value.lower() for term in terms)
        or any(keyword.lower() in value.lower() for keyword in clinical_keywords)
    ]
    if not selected:
        selected = values[:3]
    summary = "；".join(selected[:5])
    return compact(f"脱敏门诊记录摘要：{summary}", limit)


def _llm_case_summary(text: str, query: str, limit: int = 420) -> str:
    fallback = _heuristic_case_summary(text, query, limit)
    if os.getenv("PATHOLOGY_LLM_CASE_SUMMARY", "").strip().lower() not in {"1", "true", "yes"}:
        return fallback
    if not is_llm_configured("PATHOLOGY_LLM_API_KEY", "QWEN_API_KEY", "DASHSCOPE_API_KEY"):
        return fallback
    try:
        prompt = {
            "task": "将脱敏门诊原始字段改写为面向病理知识问答的证据摘要。",
            "requirements": [
                "不要输出患者编号、医生姓名、原始字段名、Unnamed 字段或流水号。",
                "不要直接复述原始门诊记录。",
                "只总结与检索问题相关的诊断线索、检查、处置或病理学相关信息。",
                "如果信息不足，明确写成'该脱敏病例仅能提示...'。",
                f"输出不超过 {limit} 个中文字符。",
            ],
            "query": query,
            "raw_case_text": compact(clean_display_text(text), 1200),
        }
        summary = chat_completion(
            [
                {"role": "system", "content": "你是医学数据脱敏与证据摘要助手，只输出一段中文摘要。"},
                {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
            ],
            temperature=0.05,
            max_tokens=420,
            api_key_names=("PATHOLOGY_LLM_API_KEY", "QWEN_API_KEY", "DASHSCOPE_API_KEY"),
            base_url_names=("PATHOLOGY_LLM_BASE_URL", "QWEN_BASE_URL", "DASHSCOPE_BASE_URL"),
            model_names=("PATHOLOGY_LLM_MODEL", "QWEN_MODEL", "DASHSCOPE_MODEL"),
        )
        summary = clean_display_text(summary, limit)
        if summary:
            return summary
    except Exception:
        return fallback
    return fallback


def relevant_excerpt(text: str, query: str, limit: int = 420) -> str:
    cleaned = compact(clean_display_text(text), max(limit * 3, limit))
    if len(cleaned) <= limit:
        return cleaned
    terms = [term for term in re.findall(r"[\u4e00-\u9fff]{2,8}|[A-Za-z][A-Za-z0-9+\-]{2,}", query or "") if term]
    best_start = 0
    best_score = -1
    step = max(60, limit // 2)
    window = min(limit * 2, 760)
    for start in range(0, max(len(cleaned), 1), step):
        snippet = cleaned[start : start + window]
        score = sum(1 for term in terms if term.lower() in snippet.lower())
        if score > best_score:
            best_score = score
            best_start = start
        if start + window >= len(cleaned):
            break
    prefix = "..." if best_start > 0 else ""
    suffix = "..." if best_start + limit < len(cleaned) else ""
    return f"{prefix}{cleaned[best_start : best_start + limit].strip()}{suffix}"


def collect_local_evidence(query: str, top_k: int) -> tuple[list[EvidenceItem], dict[str, Any]]:
    recall_k = max(18, min(50, int(top_k or 8) * 4))
    chunks = [chunk for chunk in search_knowledge_base(query, top_k=recall_k) if chunk.text]
    selected, rerank_source = rerank_chunks(query, chunks, max(1, min(int(top_k or 8), 16)))

    items: list[EvidenceItem] = []
    for index, chunk in enumerate(selected, 1):
        marker = f"[依据{index}]"
        title = clean_title(chunk.title) or clean_title(chunk.source) or "未命名依据"
        source = clean_title(chunk.source) or SOURCE_TYPE_LABELS.get(chunk.source_type, chunk.source_type)
        if chunk.source_type == "clinical_case":
            title = f"脱敏门诊病例摘要 {index}"
            excerpt = _llm_case_summary(chunk.text, query, 460)
        else:
            excerpt = relevant_excerpt(chunk.text, query, 460)
        items.append(
            EvidenceItem(
                marker=marker,
                source=source,
                source_type=chunk.source_type,
                title=title,
                excerpt=excerpt,
                relevance=explain_relevance(query, chunk),
                score=chunk.score,
                path=chunk.path,
                metadata=dict(chunk.metadata or {}),
            )
        )
    return items, {"rerank_source": rerank_source, "reranker": reranker_status(), "candidate_count": len(chunks)}


def collect_literature_evidence(articles: list[dict[str, Any]], limit: int = 5) -> list[EvidenceItem]:
    items: list[EvidenceItem] = []
    seen: set[str] = set()
    for article in articles:
        key = str(article.get("doi") or article.get("identifier") or article.get("link") or article.get("title") or "")
        if not key or key in seen:
            continue
        seen.add(key)
        index = len(items) + 1
        items.append(
            EvidenceItem(
                marker=f"[文献{index}]",
                source=str(article.get("source") or "PubMed/Elsevier"),
                source_type="literature",
                title=clean_title(str(article.get("title") or "No title")),
                excerpt=clean_display_text(str(article.get("summary") or "Abstract not available"), 520),
                relevance=f"外部文献补充，相关度 {article.get('relevance_percent') or article.get('relevance_score') or '-'}",
                score=float(article.get("relevance_score") or 0.0),
                year=str(article.get("year") or article.get("publish_date") or ""),
                journal=str(article.get("journal") or ""),
                link=str(article.get("link") or ""),
                metadata={
                    "authors": article.get("authors") or [],
                    "doi": article.get("doi"),
                    "identifier": article.get("identifier"),
                    "publish_date": article.get("publish_date"),
                },
            )
        )
        if len(items) >= limit:
            break
    return items


def evidence_prompt_block(items: list[EvidenceItem]) -> str:
    if not items:
        return "未检索到可用证据。"
    lines: list[str] = []
    for item in items:
        title = clean_title(f"{item.source} - {item.title}".strip(" -"))
        meta = []
        if item.journal:
            meta.append(item.journal)
        if item.year:
            meta.append(str(item.year))
        if item.link:
            meta.append(item.link)
        lines.append(
            f"{item.marker}\n"
            f"来源：{title}\n"
            f"类型：{SOURCE_TYPE_LABELS.get(item.source_type, item.source_type)}\n"
            f"相关性：{item.relevance}\n"
            f"补充信息：{' | '.join(meta) if meta else '-'}\n"
            f"摘录：{item.excerpt}"
        )
    return "\n\n".join(lines)
