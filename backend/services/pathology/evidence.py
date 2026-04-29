from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any

from backend.services.retrieval import EvidenceChunk, explain_relevance, rerank_chunks, reranker_status, search_knowledge_base


SOURCE_TYPE_LABELS = {
    "guideline": "指南/共识",
    "clinical_norm": "临床规范",
    "textbook": "教材",
    "general_reference": "通用医学参考",
    "clinical_case": "脱敏病例",
    "literature": "医学文献",
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
            "source": self.source,
            "source_type": self.source_type,
            "source_type_label": SOURCE_TYPE_LABELS.get(self.source_type, self.source_type),
            "title": self.title,
            "excerpt": self.excerpt,
            "relevance": self.relevance,
            "score": round(float(self.score), 4) if self.score is not None else None,
            "path": self.path,
            "year": self.year,
            "journal": self.journal,
            "link": self.link,
            "metadata": self.metadata,
        }


def compact(text: str, limit: int = 420) -> str:
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(value) <= limit:
        return value
    return value[: limit - 3].rstrip() + "..."


def relevant_excerpt(text: str, query: str, limit: int = 420) -> str:
    cleaned = compact(text, max(limit * 3, limit))
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
        marker = f"[本地{index}]"
        items.append(
            EvidenceItem(
                marker=marker,
                source=chunk.source,
                source_type=chunk.source_type,
                title=chunk.title,
                excerpt=relevant_excerpt(chunk.text, query, 460),
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
                title=str(article.get("title") or "No title"),
                excerpt=compact(str(article.get("summary") or "Abstract not available"), 520),
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
        title = f"{item.source} - {item.title}".strip(" -")
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
