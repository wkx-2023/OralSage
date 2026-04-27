from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import json
import os
import re
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

from .config import load_literature_env


load_literature_env()


PUBMED_SEARCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
PUBMED_SUMMARY_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"
PUBMED_FETCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
ELSEVIER_SEARCH_URL = "https://api.elsevier.com/content/search/scopus"

DEFAULT_MONTH_WINDOW = 12
DEFAULT_RESULT_LIMIT = 5
MAX_RESULT_LIMIT = 30
MAX_MONTH_WINDOW = 120
DEFAULT_CANDIDATE_LIMIT = 25
MAX_CANDIDATE_LIMIT = 150
HTTP_TIMEOUT_SECONDS = 20

ORAL_MEDICINE_FILTER = (
    '(oral[Title/Abstract] OR dentistry[Title/Abstract] OR dental[Title/Abstract] '
    'OR "oral medicine"[Title/Abstract] OR "oral surgery"[Title/Abstract] '
    'OR "Oral Medicine"[Mesh])'
)

QUERY_TOKEN_PATTERN = re.compile(r"[A-Za-z][A-Za-z\-]{2,}")
GENERIC_QUERY_TERMS = {
    "a",
    "an",
    "and",
    "clinical",
    "dentistry",
    "dental",
    "disease",
    "diseases",
    "effect",
    "effects",
    "health",
    "latest",
    "medical",
    "medicine",
    "meta",
    "month",
    "months",
    "new",
    "not",
    "or",
    "oral",
    "outcome",
    "outcomes",
    "patient",
    "patients",
    "progress",
    "recent",
    "research",
    "review",
    "reviews",
    "study",
    "studies",
    "summary",
    "systematic",
    "the",
    "therapy",
    "therapies",
    "treatment",
    "treatments",
    "trend",
    "trends",
    "update",
    "updates",
    "year",
    "years",
}

NEGATIVE_PATTERNS_BY_ROOT = {
    "periodont": (
        "apical periodontitis",
        "endodontic",
        "root canal",
    ),
}


@dataclass
class Article:
    title: str | None
    journal: str | None
    publish_date: str | None
    authors: list[str]
    summary: str | None
    link: str | None
    source: str
    identifier: str | None = None
    doi: str | None = None
    year: str | None = None
    relevance_score: int | None = None
    relevance_percent: int | None = None

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        if not result.get("year") and self.publish_date:
            year_match = re.search(r"\b(19|20)\d{2}\b", self.publish_date)
            if year_match:
                result["year"] = year_match.group()
        return result


class LiteratureRetrievalError(RuntimeError):
    pass


def normalize_window(months: int | None) -> int:
    value = months or DEFAULT_MONTH_WINDOW
    return max(1, min(int(value), MAX_MONTH_WINDOW))


def normalize_limit(limit: int | None) -> int:
    value = limit or DEFAULT_RESULT_LIMIT
    return max(1, min(int(value), MAX_RESULT_LIMIT))


def _candidate_limit(limit: int) -> int:
    return min(MAX_CANDIDATE_LIMIT, max(DEFAULT_CANDIDATE_LIMIT, limit * 5))


def _format_authors(authors: Any) -> list[str]:
    names: list[str] = []
    if not isinstance(authors, list):
        return names
    for author in authors:
        if isinstance(author, dict):
            name = author.get("name") or author.get("authname")
        else:
            name = str(author)
        if name:
            names.append(name)
    return names


def _normalize_text(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()


def _normalize_root(token: str) -> str:
    normalized = _normalize_text(token).replace(" ", "")
    if not normalized:
        return ""
    for suffix in (
        "ization",
        "ations",
        "ation",
        "ition",
        "ities",
        "ality",
        "ments",
        "ment",
        "ions",
        "ings",
        "ally",
        "ical",
        "itis",
        "ists",
        "isms",
        "ies",
        "ing",
        "ion",
        "ism",
        "ist",
        "ity",
        "ics",
        "ive",
        "ic",
        "al",
        "ed",
        "es",
        "s",
    ):
        if normalized.endswith(suffix) and len(normalized) - len(suffix) >= 5:
            return normalized[: -len(suffix)]
    return normalized


def _extract_query_profile(query: str) -> dict[str, Any]:
    cleaned = re.sub(r"\[[^\]]+\]", " ", query.lower())
    phrases: list[str] = []
    for raw_phrase in re.findall(r'"([^"]+)"', cleaned):
        normalized_phrase = _normalize_text(raw_phrase)
        phrase_tokens = [token for token in normalized_phrase.split() if token not in GENERIC_QUERY_TERMS]
        if phrase_tokens:
            phrases.append(" ".join(phrase_tokens))

    roots: list[str] = []
    for token in QUERY_TOKEN_PATTERN.findall(cleaned):
        token = token.lower()
        if token in GENERIC_QUERY_TERMS:
            continue
        root = _normalize_root(token)
        if len(root) >= 4 and root not in roots:
            roots.append(root)

    negative_patterns: list[str] = []
    for root in roots:
        negative_patterns.extend(NEGATIVE_PATTERNS_BY_ROOT.get(root, ()))

    return {
        "phrases": list(dict.fromkeys(phrases)),
        "roots": roots,
        "negative_patterns": list(dict.fromkeys(negative_patterns)),
    }


def _count_root_hits(words: list[str], roots: list[str]) -> int:
    matched = 0
    for root in roots:
        if any(word.startswith(root) for word in words):
            matched += 1
    return matched


def _article_relevance_score(article: Article, query_profile: dict[str, Any]) -> int:
    title = _normalize_text(article.title)
    summary = _normalize_text(article.summary)
    title_words = title.split()
    summary_words = summary.split()

    title_phrase_hits = sum(1 for phrase in query_profile["phrases"] if phrase in title)
    summary_phrase_hits = sum(1 for phrase in query_profile["phrases"] if phrase in summary)
    title_root_hits = _count_root_hits(title_words, query_profile["roots"])
    summary_root_hits = _count_root_hits(summary_words, query_profile["roots"])

    strong_title_match = title_phrase_hits > 0 or title_root_hits > 0
    strong_summary_match = summary_phrase_hits >= 2 or summary_root_hits >= 2
    if not strong_title_match and not strong_summary_match:
        return 0

    score = (title_phrase_hits * 8) + (title_root_hits * 5)
    score += (summary_phrase_hits * 2) + min(summary_root_hits, 3)

    combined_text = f"{title} {summary}".strip()
    for pattern in query_profile["negative_patterns"]:
        if pattern and pattern in combined_text:
            score -= 10
    return max(score, 0)


def _dedupe_articles(articles: list[Article]) -> list[Article]:
    deduped: list[Article] = []
    seen: set[str] = set()
    for article in articles:
        dedupe_key = article.doi or article.identifier or _normalize_text(article.title) or article.link or ""
        if not dedupe_key or dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        deduped.append(article)
    return deduped


def _filter_articles_by_query(articles: list[Article], query: str, limit: int) -> list[Article]:
    query_profile = _extract_query_profile(query)
    if not query_profile["roots"] and not query_profile["phrases"]:
        deduped = _dedupe_articles(articles)[:limit]
        for article in deduped:
            article.relevance_score = 0
            article.relevance_percent = None
        return deduped

    scored_articles: list[tuple[int, int, Article]] = []
    for index, article in enumerate(articles):
        score = _article_relevance_score(article, query_profile)
        if score > 0:
            scored_articles.append((score, index, article))

    if not scored_articles:
        return []

    max_score = max(score for score, _, _ in scored_articles) or 1
    for score, _, article in scored_articles:
        article.relevance_score = score
        article.relevance_percent = max(1, min(100, round((score / max_score) * 100)))

    scored_articles.sort(key=lambda item: (-item[0], item[1]))
    return _dedupe_articles([article for _, _, article in scored_articles])[:limit]


def merge_articles(primary: list[Article], supplemental: list[Article], limit: int) -> list[Article]:
    return _dedupe_articles(primary + supplemental)[:limit]


def _request_text(url: str, params: dict[str, Any] | None = None, headers: dict[str, str] | None = None) -> str:
    query = f"?{urlencode(params)}" if params else ""
    request = Request(url + query, headers=headers or {"User-Agent": "OralPath-Rebuild/0.1"})
    try:
        with urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            return response.read().decode("utf-8", errors="replace")
    except (HTTPError, URLError, TimeoutError) as exc:
        raise LiteratureRetrievalError(str(exc)) from exc


def _extract_pubmed_abstracts(fetch_xml: str) -> dict[str, str]:
    abstract_map: dict[str, str] = {}
    try:
        root = ET.fromstring(fetch_xml)
    except ET.ParseError:
        return abstract_map

    for article in root.findall(".//PubmedArticle"):
        pmid_el = article.find(".//PMID")
        if pmid_el is None or not pmid_el.text:
            continue
        pmid = pmid_el.text.strip()
        abstract_texts: list[str] = []
        for ab in article.findall(".//Abstract/AbstractText"):
            label = ab.get("Label")
            text = "".join(ab.itertext()).strip()
            if not text:
                continue
            abstract_texts.append(f"{label}: {text}" if label else text)
        if abstract_texts:
            abstract_map[pmid] = "\n".join(abstract_texts)
    return abstract_map


def _fetch_pubmed_articles_sync(query: str, months: int, limit: int) -> list[Article]:
    contact_email = os.getenv("PUBMED_CONTACT_EMAIL", "")
    candidate_limit = _candidate_limit(limit)
    filtered_term = f"({query}) AND {ORAL_MEDICINE_FILTER}"

    search_params = {
        "db": "pubmed",
        "term": filtered_term,
        "sort": "pub+date",
        "reldate": str(months * 30),
        "retmode": "json",
        "retmax": str(candidate_limit),
    }
    if contact_email:
        search_params["email"] = contact_email

    search_body = _request_text(PUBMED_SEARCH_URL, search_params)
    search_data = json.loads(search_body).get("esearchresult", {})
    id_list = search_data.get("idlist", [])

    if not id_list:
        search_params["term"] = query
        search_body = _request_text(PUBMED_SEARCH_URL, search_params)
        search_data = json.loads(search_body).get("esearchresult", {})
        id_list = search_data.get("idlist", [])

    if not id_list:
        return []

    id_str = ",".join(id_list)
    summary_body = _request_text(
        PUBMED_SUMMARY_URL,
        {"db": "pubmed", "id": id_str, "retmode": "json"},
    )
    summary_data = json.loads(summary_body).get("result", {})

    fetch_params = {"db": "pubmed", "id": id_str, "retmode": "xml"}
    if contact_email:
        fetch_params["email"] = contact_email
    fetch_xml = _request_text(PUBMED_FETCH_URL, fetch_params)
    abstract_map = _extract_pubmed_abstracts(fetch_xml)

    articles: list[Article] = []
    for uid in summary_data.get("uids", []):
        record = summary_data.get(uid, {})
        if not record:
            continue

        abstract_text = abstract_map.get(uid)
        if not abstract_text:
            abstract = record.get("abstract")
            if isinstance(abstract, str):
                abstract_text = abstract
            elif isinstance(abstract, dict):
                abstract_text = abstract.get("AbstractText") or abstract.get("text")
        if not abstract_text:
            abstract_text = record.get("elocationid") or record.get("sortpubdate") or "Abstract not available"

        publish_date = record.get("pubdate") or record.get("sortpubdate") or ""
        year_match = re.search(r"\b(19|20)\d{2}\b", publish_date)

        articles.append(
            Article(
                identifier=uid,
                title=record.get("title") or "No title",
                journal=record.get("fulljournalname") or record.get("source"),
                publish_date=publish_date,
                authors=_format_authors(record.get("authors")),
                summary=abstract_text,
                link=f"https://pubmed.ncbi.nlm.nih.gov/{uid}/",
                source="PubMed",
                year=year_match.group() if year_match else None,
            )
        )

    return _filter_articles_by_query(articles, query, limit)


def _scopus_query(query: str) -> str:
    oral_filter = 'oral OR dental OR dentistry OR "oral medicine" OR "oral surgery"'
    return f"TITLE-ABS-KEY(({query}) AND ({oral_filter}))"


def _fetch_elsevier_articles_sync(
    query: str,
    months: int,
    limit: int,
    api_key: str | None = None,
    institution_token: str | None = None,
) -> list[Article]:
    key = api_key or os.getenv("ELSEVIER_API_KEY")
    if not key:
        raise LiteratureRetrievalError("ELSEVIER_API_KEY is not configured")

    start_date = datetime.now(timezone.utc).date() - timedelta(days=months * 30)
    headers = {
        "Accept": "application/json",
        "X-ELS-APIKey": key,
        "User-Agent": "OralPath-Rebuild/0.1",
    }
    token = institution_token or os.getenv("ELSEVIER_INSTITUTION_TOKEN")
    if token:
        headers["X-ELS-Insttoken"] = token

    body = _request_text(
        ELSEVIER_SEARCH_URL,
        {
            "query": _scopus_query(query),
            "date": start_date.strftime("%Y-%m-%d"),
            "sort": "coverDate",
            "count": str(_candidate_limit(limit)),
        },
        headers=headers,
    )
    entries = json.loads(body).get("search-results", {}).get("entry", []) or []

    articles: list[Article] = []
    for entry in entries:
        link = None
        for candidate in entry.get("link") or []:
            if candidate.get("@rel") in {"scopus", "via"}:
                link = candidate.get("@href")
                break

        publish_date = entry.get("prism:coverDate") or ""
        year_match = re.search(r"\b(19|20)\d{2}\b", publish_date)

        articles.append(
            Article(
                identifier=entry.get("eid"),
                title=entry.get("dc:title"),
                journal=entry.get("prism:publicationName"),
                publish_date=publish_date,
                authors=_format_authors(entry.get("author")),
                summary=entry.get("dc:description") or "Abstract not available",
                link=link,
                doi=entry.get("prism:doi") or entry.get("dc:identifier"),
                source="Elsevier",
                year=year_match.group() if year_match else None,
            )
        )

    return _filter_articles_by_query(articles, query, limit)


async def search_pubmed_oral_medicine(query: str, months: int | None = None, limit: int | None = None) -> list[dict[str, Any]]:
    normalized_months = normalize_window(months)
    normalized_limit = normalize_limit(limit)
    articles = await asyncio.to_thread(_fetch_pubmed_articles_sync, query, normalized_months, normalized_limit)
    return [article.to_dict() for article in articles]


async def search_elsevier_oral_medicine(
    query: str,
    months: int | None = None,
    limit: int | None = None,
    api_key: str | None = None,
    institution_token: str | None = None,
) -> list[dict[str, Any]]:
    normalized_months = normalize_window(months)
    normalized_limit = normalize_limit(limit)
    articles = await asyncio.to_thread(
        _fetch_elsevier_articles_sync,
        query,
        normalized_months,
        normalized_limit,
        api_key,
        institution_token,
    )
    return [article.to_dict() for article in articles]
