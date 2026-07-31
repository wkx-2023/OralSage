from __future__ import annotations

import asyncio
import re
from typing import Any, Awaitable, Callable

from .retrieval import LiteratureRetrievalError, merge_articles, search_elsevier_oral_medicine, search_pubmed_oral_medicine
from .summarizer import build_review, translate_article
from .terms import resolve_terms


FRONTIER_AGENT_NAMES = {
    "oral_medicine_search_assistant",
    "oral_frontier_learning_assistant",
    "frontier_literature",
}


def run_terms(task: str, months: int | None = None) -> dict[str, Any]:
    return resolve_terms(task, months).to_dict()


def _article_dicts_to_mergeable(articles: list[dict[str, Any]]):
    from .retrieval import Article

    converted = []
    for article in articles:
        converted.append(
            Article(
                title=article.get("title"),
                journal=article.get("journal"),
                publish_date=article.get("publish_date"),
                authors=article.get("authors") or [],
                summary=article.get("summary"),
                link=article.get("link"),
                source=article.get("source") or "Unknown",
                identifier=article.get("identifier"),
                doi=article.get("doi"),
                year=article.get("year"),
                relevance_score=article.get("relevance_score"),
                relevance_percent=article.get("relevance_percent"),
            )
        )
    return converted


def _clean_text(text: Any) -> str:
    value = _strip_emoji(text)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def _clip(text: Any, limit: int = 360) -> str:
    value = _clean_text(text)
    if not value:
        return "暂无摘要。"
    if len(value) <= limit:
        return value
    return value[: limit - 3].rstrip() + "..."


def _format_authors(authors: Any) -> str:
    if not isinstance(authors, list) or not authors:
        return "未提供作者"
    visible = [_clean_text(author) for author in authors[:4] if _clean_text(author)]
    if not visible:
        return "未提供作者"
    return "、".join(visible) + (" 等" if len(authors) > len(visible) else "")


def _strip_emoji(text: Any) -> str:
    value = str(text or "")
    value = re.sub(r"[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0E\uFE0F\u200D]", "", value)
    value = re.sub(r"[ \t]{2,}", " ", value)
    return value


def _translated_value(article: dict[str, Any], key: str) -> str:
    translation = article.get("translation")
    if not isinstance(translation, dict) or translation.get("source") != "llm":
        return ""
    return _strip_emoji(translation.get(key)).strip()


async def _with_chat_translations(articles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    translations = await asyncio.gather(*(asyncio.to_thread(translate_article, article) for article in articles))
    translated_articles: list[dict[str, Any]] = []
    for article, translation in zip(articles, translations, strict=False):
        item = dict(article)
        item["translation"] = translation
        translated_articles.append(item)
    return translated_articles


def _format_chat_answer(
    query: str,
    terms: dict[str, Any],
    articles: list[dict[str, Any]],
    review: str,
    review_source: str,
    sources: list[str],
    errors: list[str],
    requested_limit: int,
) -> str:
    months = terms.get("recommended_months") or "-"
    keywords = terms.get("recommended_keywords") or query
    source_text = " + ".join(sources) if sources else "未获得可用来源"
    count_text = f"{len(articles)} 篇" if len(articles) == requested_limit else f"{len(articles)} 篇（请求 {requested_limit} 篇）"

    lines = [
        "## 文献检索结果",
        f"已按你的问题“{query}”检索近 {months} 个月文献，当前获得 {count_text}。",
        f"- 检索关键词：`{keywords}`",
        f"- 数据来源：{source_text}",
        "",
    ]

    if not articles:
        lines.extend(
            [
                "未检索到可展示的真实文献结果。建议放宽时间范围、减少限定词，或检查 PubMed/Elsevier 网络与密钥配置。",
            ]
        )
        if errors:
            lines.extend(["", "### 检索提示", *[f"- {error}" for error in errors]])
        return _strip_emoji("\n".join(lines))

    lines.append("### 文献清单")
    for index, article in enumerate(articles, 1):
        title = _clean_text(article.get("title") or "No title")
        title_zh = _clean_text(_translated_value(article, "title_zh"))
        summary_zh = _translated_value(article, "summary_zh")
        journal = _clean_text(article.get("journal") or "Unknown journal")
        date = _clean_text(article.get("publish_date") or article.get("year") or "Unknown date")
        link = _clean_text(article.get("link") or "")
        doi = _clean_text(article.get("doi") or "")
        source = _clean_text(article.get("source") or "Unknown source")
        score = article.get("relevance_percent")

        lines.extend(
            [
                f"### {index}. {title_zh or title}",
                f"**英文标题：** {title}",
                f"**来源：** {source}；**期刊/日期：** {journal}，{date}",
                f"**作者：** {_format_authors(article.get('authors'))}",
                f"**中文摘要：** {_clip(summary_zh) if summary_zh else '暂未生成中文摘要翻译。'}",
                f"**英文摘要：** {_clip(article.get('summary'))}",
            ]
        )
        if isinstance(score, (int, float)) and score > 0:
            lines.append(f"**相关度：** {round(score)}%")
        if doi:
            lines.append(f"**DOI：** {doi}")
        if link:
            lines.append(f"**原文链接：** {link}")
        lines.append("")

    if review_source == "llm" and review.strip():
        lines.extend(["### 综合解读", _strip_emoji(review).strip(), ""])
    else:
        lines.extend(
            [
                "### 综合解读",
                "以上为真实文献检索结果。若需要更严格的证据分级或中文综述，请在配置文献总结模型后再生成完整综述。",
                "",
            ]
        )

    visible_errors = [error for error in errors if "ELSEVIER_API_KEY is not configured" not in error]
    if visible_errors:
        lines.extend(["### 检索提示", *[f"- {error}" for error in visible_errors], ""])

    lines.append("内容仅用于口腔医学学习与科研参考，不构成临床诊疗建议。")
    return _strip_emoji("\n".join(lines)).strip()


async def run_literature_search(
    query: str,
    months: int | None = None,
    limit: int = 5,
    *,
    include_chat_translations: bool = False,
    tool_caller: Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    query = query.strip()
    if not query:
        return {
            "ok": False,
            "module": "frontier_literature",
            "message": "query is required",
            "terms": {},
            "articles": [],
            "report": "",
            "sources": [],
            "errors": ["query is required"],
        }

    if tool_caller:
        term_result = await tool_caller(
            "resolve_oral_terms",
            {"query": query, "months": months},
        )
        terms = term_result.get("result") if isinstance(term_result, dict) else None
        if not isinstance(terms, dict):
            raise RuntimeError("MCP resolve_oral_terms returned an invalid result")
    else:
        terms = run_terms(query, months)
    keywords = str(terms.get("recommended_keywords") or query)
    search_months = int(terms.get("recommended_months") or months or 12)
    search_limit = max(1, min(int(limit or 5), 30))

    errors: list[str] = []
    sources: list[str] = []
    pubmed_articles: list[dict[str, Any]] = []
    elsevier_articles: list[dict[str, Any]] = []

    try:
        if tool_caller:
            pubmed_result = await tool_caller(
                "search_pubmed_oral_medicine",
                {"query": keywords, "months": search_months, "limit": search_limit},
            )
            pubmed_articles = pubmed_result.get("articles", [])
        else:
            pubmed_articles = await search_pubmed_oral_medicine(keywords, search_months, search_limit)
        if pubmed_articles:
            sources.append("PubMed")
    except LiteratureRetrievalError as exc:
        errors.append(f"PubMed failed: {exc}")
    except Exception as exc:
        errors.append(f"PubMed failed: {exc}")

    if len(pubmed_articles) < search_limit:
        try:
            if tool_caller:
                elsevier_result = await tool_caller(
                    "search_elsevier_oral_medicine",
                    {
                        "query": keywords,
                        "months": search_months,
                        "limit": search_limit - len(pubmed_articles),
                    },
                )
                elsevier_articles = elsevier_result.get("articles", [])
            else:
                elsevier_articles = await search_elsevier_oral_medicine(
                    keywords,
                    search_months,
                    search_limit - len(pubmed_articles),
                )
            if elsevier_articles:
                sources.append("Scopus")
        except LiteratureRetrievalError as exc:
            errors.append(f"Elsevier skipped/failed: {exc}")
        except Exception as exc:
            errors.append(f"Elsevier failed: {exc}")

    merged = merge_articles(
        _article_dicts_to_mergeable(pubmed_articles),
        _article_dicts_to_mergeable(elsevier_articles),
        search_limit,
    )
    articles = [article.to_dict() for article in merged]
    review, review_source = build_review(query, terms, articles, sources, errors)
    answer_articles = await _with_chat_translations(articles) if include_chat_translations else articles
    answer = _format_chat_answer(
        query=query,
        terms=terms,
        articles=answer_articles,
        review=review,
        review_source=review_source,
        sources=sources,
        errors=errors,
        requested_limit=search_limit,
    )

    return {
        "ok": True,
        "module": "frontier_literature",
        "query": query,
        "terms": terms,
        "articles": articles,
        "review": review,
        "review_source": review_source,
        "report": review,
        "report_source": review_source,
        "answer": answer,
        "chat_articles": answer_articles,
        "sources": sources,
        "errors": errors,
    }


def run_article_translation(article: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": True,
        "module": "literature_article_translation",
        "translation": translate_article(article),
    }
