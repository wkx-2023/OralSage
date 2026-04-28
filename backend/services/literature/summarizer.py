from __future__ import annotations

import json
import os
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .config import load_literature_env


load_literature_env()

DEFAULT_LLM_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEFAULT_LLM_MODEL = "qwen-plus"
MAX_REVIEW_ARTICLES = 12


def _env(*names: str) -> str | None:
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    return None


def _llm_settings() -> tuple[str | None, str, str]:
    return (
        _env("LITERATURE_LLM_API_KEY", "QWEN_API_KEY", "DASHSCOPE_API_KEY"),
        (_env("LITERATURE_LLM_BASE_URL", "QWEN_BASE_URL", "DASHSCOPE_BASE_URL") or DEFAULT_LLM_BASE_URL).rstrip("/"),
        _env("LITERATURE_LLM_MODEL", "QWEN_MODEL", "DASHSCOPE_MODEL") or DEFAULT_LLM_MODEL,
    )


def llm_enabled() -> bool:
    return bool(_llm_settings()[0])


def _clip(text: str | None, limit: int = 1200) -> str:
    value = (text or "").strip()
    if len(value) <= limit:
        return value
    return value[: limit - 3].rstrip() + "..."


def _authors(authors: Any) -> str:
    if not isinstance(authors, list) or not authors:
        return "Not provided"
    visible = [str(author) for author in authors[:3] if author]
    return ", ".join(visible) + (" et al." if len(authors) > 3 else "")


def _chat_completion(messages: list[dict[str, str]], *, temperature: float = 0.15, max_tokens: int = 2200) -> str:
    api_key, base_url, model = _llm_settings()
    if not api_key:
        raise RuntimeError("QWEN_API_KEY or LITERATURE_LLM_API_KEY is not configured")

    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    request = Request(
        f"{base_url}/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "OralPath-Rebuild/0.1",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=90) as response:
            data = json.loads(response.read().decode("utf-8", errors="replace"))
    except (HTTPError, URLError, TimeoutError) as exc:
        raise RuntimeError(str(exc)) from exc

    try:
        return data["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"Unexpected LLM response shape: {data}") from exc


def _json_from_text(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        stripped = stripped.removeprefix("json").strip()
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start >= 0 and end > start:
        stripped = stripped[start : end + 1]
    return json.loads(stripped)


def _article_payload(article: dict[str, Any], index: int | None = None) -> dict[str, Any]:
    payload = {
        "title": article.get("title"),
        "journal": article.get("journal"),
        "publish_date": article.get("publish_date") or article.get("year"),
        "authors": article.get("authors"),
        "summary": _clip(article.get("summary"), 1400),
        "link": article.get("link"),
        "source": article.get("source"),
    }
    if index is not None:
        payload["index"] = index
    return payload


def fallback_translation(article: dict[str, Any], error: str | None = None) -> dict[str, Any]:
    return {
        "title_zh": "未配置或未成功调用大模型，暂无法生成中文标题。",
        "summary_zh": "未配置或未成功调用大模型，暂无法生成中文摘要翻译。",
        "key_points": [],
        "source": "fallback",
        "error": error,
    }


def translate_article(article: dict[str, Any]) -> dict[str, Any]:
    if not llm_enabled():
        return fallback_translation(article)

    system_prompt = (
        "You are a medical literature translation assistant for oral medicine. "
        "Translate only from the provided title and abstract/description. "
        "Do not add facts that are not in the input. "
        "Return strict JSON with keys: title_zh, summary_zh, key_points. "
        "title_zh must be concise Chinese. summary_zh must be faithful Chinese translation. "
        "key_points must be 2-4 short Chinese bullets based only on the abstract."
    )
    try:
        text = _chat_completion(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(_article_payload(article), ensure_ascii=False)},
            ],
            max_tokens=1800,
        )
        data = _json_from_text(text)
        return {
            "title_zh": str(data.get("title_zh") or ""),
            "summary_zh": str(data.get("summary_zh") or ""),
            "key_points": data.get("key_points") if isinstance(data.get("key_points"), list) else [],
            "source": "llm",
            "error": None,
        }
    except Exception as exc:
        return fallback_translation(article, str(exc))


def fallback_review(
    user_query: str,
    terms: dict[str, Any],
    articles: list[dict[str, Any]],
    sources: list[str],
    errors: list[str] | None = None,
) -> str:
    lines = [
        "## 检索综述",
        f"- 主题：{user_query}",
        f"- 英文关键词：{terms.get('recommended_keywords') or '-'}",
        f"- 时间窗口：{terms.get('recommended_months') or '-'} 个月",
        f"- 数据库：{' + '.join(sources) if sources else '无可用结果'}",
        "",
    ]
    if not articles:
        lines.extend(
            [
                "本次没有获取到可用真实文献，因此不生成趋势性结论。",
                "内容仅用于口腔医学学习与科研参考，不构成临床诊疗建议。",
            ]
        )
        if errors:
            lines.append(f"检索提示：{'; '.join(errors)}")
        return "\n".join(lines)

    lines.append("已获取真实文献。当前综述为保守规则版：请查看单篇摘要或配置 Qwen 生成完整综述。")
    for idx, article in enumerate(articles[:5], 1):
        lines.append(
            f"{idx}. {article.get('title') or 'No title'} "
            f"({article.get('journal') or 'Unknown journal'}, {article.get('publish_date') or article.get('year') or 'Unknown date'})"
        )
    lines.append("")
    lines.append("内容仅用于口腔医学学习与科研参考，不构成临床诊疗建议。")
    if errors:
        lines.append(f"检索提示：{'; '.join(errors)}")
    return "\n".join(lines)


def build_review(
    user_query: str,
    terms: dict[str, Any],
    articles: list[dict[str, Any]],
    sources: list[str],
    errors: list[str] | None = None,
) -> tuple[str, str]:
    if not llm_enabled() or not articles:
        return fallback_review(user_query, terms, articles, sources, errors), "fallback"

    compact_articles = [_article_payload(article, idx) for idx, article in enumerate(articles[:MAX_REVIEW_ARTICLES], 1)]
    system_prompt = (
        "You are an oral medicine frontier literature review assistant. "
        "Write in Chinese. Use only the provided real article titles and abstracts. "
        "Do not invent authors, journals, data, drugs, outcomes, or clinical recommendations. "
        "Output Markdown with these sections: "
        "1) 检索概况, 2) 研究主题分组, 3) 主要发现, 4) 证据局限, 5) 学习与科研提示, 6) 免责声明. "
        "Keep it concise and useful for dentists or oral medicine students."
    )
    user_payload = {
        "user_query": user_query,
        "terms": terms,
        "sources": sources,
        "article_count": len(articles),
        "reviewed_articles": compact_articles,
    }
    try:
        review = _chat_completion(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
            ],
            max_tokens=2600,
        )
        return review, "llm"
    except Exception as exc:
        merged_errors = [*(errors or []), f"LLM review failed: {exc}"]
        return fallback_review(user_query, terms, articles, sources, merged_errors), "fallback"


def build_report(
    user_query: str,
    terms: dict[str, Any],
    articles: list[dict[str, Any]],
    sources: list[str],
    errors: list[str] | None = None,
) -> tuple[str, str]:
    return build_review(user_query, terms, articles, sources, errors)
