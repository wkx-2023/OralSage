from __future__ import annotations

from typing import Any

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


async def run_literature_search(query: str, months: int | None = None, limit: int = 5) -> dict[str, Any]:
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

    terms = run_terms(query, months)
    keywords = str(terms.get("recommended_keywords") or query)
    search_months = int(terms.get("recommended_months") or months or 12)
    search_limit = max(1, min(int(limit or 5), 30))

    errors: list[str] = []
    sources: list[str] = []
    pubmed_articles: list[dict[str, Any]] = []
    elsevier_articles: list[dict[str, Any]] = []

    try:
        pubmed_articles = await search_pubmed_oral_medicine(keywords, search_months, search_limit)
        if pubmed_articles:
            sources.append("PubMed")
    except LiteratureRetrievalError as exc:
        errors.append(f"PubMed failed: {exc}")
    except Exception as exc:
        errors.append(f"PubMed failed: {exc}")

    if len(pubmed_articles) < search_limit:
        try:
            elsevier_articles = await search_elsevier_oral_medicine(
                keywords,
                search_months,
                search_limit - len(pubmed_articles),
            )
            if elsevier_articles:
                sources.append("Elsevier")
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
        "sources": sources,
        "errors": errors,
    }


def run_article_translation(article: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": True,
        "module": "literature_article_translation",
        "translation": translate_article(article),
    }
