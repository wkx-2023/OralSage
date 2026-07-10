"""Real LLM client adapter for Pin & Paper planner.

Provides an OpenAI-compatible LLM client that can be injected into
``plan_pin_and_paper_slide()`` / ``plan_pin_and_paper_deck()``.

Configuration priority (all from environment variables):
1. ``PIN_AND_PAPER_LLM_API_KEY`` → ``OPENAI_API_KEY`` → ``LEARNING_LLM_API_KEY``
2. ``PIN_AND_PAPER_LLM_MODEL`` → ``deepseek-chat`` (PPT专用默认，不跟随全局 LEARNING_LLM_MODEL)
3. ``PIN_AND_PAPER_LLM_BASE_URL`` → ``LEARNING_LLM_BASE_URL`` → ``https://api.deepseek.com``
4. ``PIN_AND_PAPER_LLM_MAX_CONCURRENCY`` — optional; defaults to 3
5. ``PIN_AND_PAPER_LLM_TEMPERATURE`` — optional; defaults to 0.3
6. ``PIN_AND_PAPER_LLM_MAX_TOKENS`` — optional; defaults to 4096
7. ``PIN_AND_PAPER_LLM_TIMEOUT`` — optional; defaults to 120 (seconds)
8. ``PIN_AND_PAPER_LLM_MAX_RETRIES`` — optional; defaults to 2 (per request, for transient errors)

Public API
----------
* ``create_real_llm_client()`` — returns a ``(prompt: str) -> str`` callable or None
* ``plan_deck_concurrent()`` — concurrent per-slide planning with real LLM
* ``ensure_project_env()`` — load .env from project root (idempotent)

Architecture
------------
This module is a thin adapter — it does NOT embed provider logic in the planner.
The planner receives a callable ``(prompt: str) -> str`` and does not know about
HTTP, OpenAI, or any other provider details.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Callable

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# .env loading (idempotent)
# ---------------------------------------------------------------------------

_ENV_LOADED = False


def ensure_project_env() -> None:
    """Load .env from project root into os.environ, once per process."""
    global _ENV_LOADED
    if _ENV_LOADED:
        return
    try:
        from backend.services.env_loader import load_project_env
        load_project_env()
    except ImportError:
        # Fallback: manual .env load from project root
        _manual_load_dotenv()
    _ENV_LOADED = True


def _manual_load_dotenv() -> None:
    """Minimal .env loader when env_loader module is unavailable."""
    from pathlib import Path

    candidates = [
        Path(__file__).resolve().parents[4] / ".env",
        Path(".env"),
    ]
    for dotenv_path in candidates:
        if not dotenv_path.exists():
            continue
        for raw_line in dotenv_path.read_text(encoding="utf-8-sig").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[7:].lstrip()
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip("'").strip('"')
            if key and key not in os.environ:
                os.environ[key] = value
        break


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_DEFAULT_MODEL = "deepseek-chat"
_DEFAULT_BASE_URL = "https://api.deepseek.com"
_DEFAULT_MAX_CONCURRENCY = 3
_DEFAULT_TEMPERATURE = 0.3
_DEFAULT_MAX_TOKENS = 4096
_DEFAULT_TIMEOUT = 120.0
_DEFAULT_MAX_RETRIES = 2


def _get_config() -> dict[str, Any]:
    """Resolve configuration with project-aware fallback chain.

    Priority: PIN_AND_PAPER_LLM_* > OPENAI_API_KEY > LEARNING_LLM_* > defaults
    """
    # API key: PIN_AND_PAPER specific → OPENAI → LEARNING fallback
    api_key = (
        os.environ.get("PIN_AND_PAPER_LLM_API_KEY", "")
        or os.environ.get("OPENAI_API_KEY", "")
        or os.environ.get("LEARNING_LLM_API_KEY", "")
    )

    # Model: PIN_AND_PAPER specific only (never fallback to LEARNING_LLM_MODEL)
    model = os.environ.get("PIN_AND_PAPER_LLM_MODEL", "") or _DEFAULT_MODEL

    # Base URL: PIN_AND_PAPER specific → LEARNING_LLM_BASE_URL → default
    base_url = (
        os.environ.get("PIN_AND_PAPER_LLM_BASE_URL", "")
        or os.environ.get("LEARNING_LLM_BASE_URL", "")
        or _DEFAULT_BASE_URL
    )

    return {
        "api_key": api_key,
        "model": model,
        "base_url": base_url,
        "max_concurrency": int(
            os.environ.get("PIN_AND_PAPER_LLM_MAX_CONCURRENCY", str(_DEFAULT_MAX_CONCURRENCY))
        ),
        "temperature": float(
            os.environ.get("PIN_AND_PAPER_LLM_TEMPERATURE", str(_DEFAULT_TEMPERATURE))
        ),
        "max_tokens": int(
            os.environ.get("PIN_AND_PAPER_LLM_MAX_TOKENS", str(_DEFAULT_MAX_TOKENS))
        ),
        "timeout": float(
            os.environ.get("PIN_AND_PAPER_LLM_TIMEOUT", str(_DEFAULT_TIMEOUT))
        ),
        "max_retries": int(
            os.environ.get("PIN_AND_PAPER_LLM_MAX_RETRIES", str(_DEFAULT_MAX_RETRIES))
        ),
    }


# ---------------------------------------------------------------------------
# OpenAI-compatible client factory
# ---------------------------------------------------------------------------


def create_real_llm_client() -> Callable[[str], str] | None:
    """Create an OpenAI-compatible LLM client callable, or None if no API key.

    Returns
    -------
    A ``(prompt: str) -> str`` callable that makes synchronous HTTP requests
    to the configured OpenAI-compatible endpoint.  Returns ``None`` if
    ``OPENAI_API_KEY`` is not set (caller must handle by skipping or failing).
    """
    import urllib.request
    import urllib.error

    config = _get_config()
    api_key = config["api_key"]
    if not api_key:
        logger.warning(
            "No API key found (checked PIN_AND_PAPER_LLM_API_KEY, OPENAI_API_KEY, "
            "LEARNING_LLM_API_KEY) — real LLM client unavailable. "
            "Skipping real LLM calls."
        )
        return None

    model = config["model"]
    base_url = config["base_url"].rstrip("/")
    temperature = config["temperature"]
    max_tokens = config["max_tokens"]
    timeout = config["timeout"]
    max_retries = config["max_retries"]

    url = f"{base_url}/chat/completions"

    def _llm_client(prompt: str) -> str:
        payload = json.dumps({
            "model": model,
            "messages": [
                {"role": "system", "content": "You are a precise PPT slide planner. Return valid JSON only."},
                {"role": "user", "content": prompt},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }).encode("utf-8")

        last_error: Exception | None = None
        for attempt in range(max_retries + 1):
            try:
                req = urllib.request.Request(
                    url,
                    data=payload,
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {api_key}",
                    },
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    body = json.loads(resp.read().decode("utf-8"))
                    content = body["choices"][0]["message"]["content"]
                    return content
            except (urllib.error.HTTPError, urllib.error.URLError, OSError,
                    json.JSONDecodeError, KeyError, IndexError) as exc:
                last_error = exc
                if attempt < max_retries:
                    wait = 2 ** attempt
                    logger.warning(
                        f"LLM request attempt {attempt + 1} failed: {exc}. "
                        f"Retrying in {wait}s..."
                    )
                    time.sleep(wait)
                else:
                    logger.error(
                        f"LLM request failed after {max_retries + 1} attempts: {exc}"
                    )

        raise RuntimeError(
            f"Real LLM client failed after {max_retries + 1} attempts: {last_error}"
        )

    return _llm_client


# ---------------------------------------------------------------------------
# Concurrent deck planning
# ---------------------------------------------------------------------------


def _plan_one_slide_with_client(
    source_slide: dict,
    slide_types: dict,
    llm_client: Callable[[str], str],
    slide_index: int,
    total_slides: int,
) -> dict:
    """Plan a single slide (imports planner lazily to avoid circular imports)."""
    from backend.services.learning.pin_and_paper_planner import plan_pin_and_paper_slide

    return plan_pin_and_paper_slide(
        source_slide=source_slide,
        slide_types=slide_types,
        llm_client=llm_client,
        max_retries=1,
    )


def plan_deck_concurrent(
    deck: dict,
    slide_types: dict | None = None,
    llm_client: Callable[[str], str] | None = None,
    max_concurrency: int | None = None,
) -> dict:
    """Plan all slides in a ``slides.v1`` deck using concurrent real LLM calls.

    Each slide is planned independently (with its own validation, budget check,
    and compression retry).  Results are aggregated in outline order.

    If ``llm_client`` is None, the client returned by :func:`create_real_llm_client`
    is used (or an error is raised if no API key is set).

    Parameters
    ----------
    deck:
        A ``slides.v1`` dict.
    slide_types:
        Pre-loaded Pin & Paper slide-types dict.
    llm_client:
        Injectable LLM client callable.  Each call is synchronous; concurrency
        happens at the thread level via ``concurrent.futures``.
    max_concurrency:
        Maximum concurrent LLM calls.  Defaults to ``PIN_AND_PAPER_LLM_MAX_CONCURRENCY``
        env var or 3.

    Returns
    -------
    A ``pin_and_paper_slide_plan.v1`` dict with ``slides`` in outline order,
    ``warnings`` aggregated, and ``_llm_calls`` total count.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from backend.services.learning.pin_and_paper_planner import load_slide_types

    if llm_client is None:
        llm_client = create_real_llm_client()
        if llm_client is None:
            raise ValueError(
                "No LLM client available — set PIN_AND_PAPER_LLM_API_KEY, "
                "OPENAI_API_KEY, or LEARNING_LLM_API_KEY environment variable"
            )

    if slide_types is None:
        slide_types = load_slide_types()

    if max_concurrency is None:
        max_concurrency = _get_config()["max_concurrency"]

    source_slides = deck.get("slides", [])
    total = len(source_slides)

    # Map slide_index -> result dict, then sort by index at the end
    results_by_index: dict[int, dict] = {}
    all_warnings: list[str] = list(deck.get("warnings", []))
    total_llm_calls = 0

    with ThreadPoolExecutor(max_workers=max_concurrency) as executor:
        futures = {
            executor.submit(
                _plan_one_slide_with_client,
                source_slide=slide,
                slide_types=slide_types,
                llm_client=llm_client,
                slide_index=i,
                total_slides=total,
            ): i
            for i, slide in enumerate(source_slides)
        }

        for future in as_completed(futures):
            idx = futures[future]
            try:
                result = future.result()
            except Exception as exc:
                sid = source_slides[idx].get("slide_id", f"slide_{idx}")
                oid = source_slides[idx].get("outline_node_id", "unknown")
                results_by_index[idx] = {
                    "slide_plan": {
                        "source_slide_id": sid,
                        "source_outline_node_id": oid,
                        "page_type": "",
                        "rationale": "",
                        "slots": {},
                        "content_warnings": [],
                    },
                    "warnings": [f"concurrent_llm_call_failed: slide {idx} — {exc}"],
                    "llm_calls": 0,
                }
            else:
                results_by_index[idx] = result

    # Aggregate in order
    slides: list[dict] = []
    for i in range(total):
        result = results_by_index.get(i)
        if result is None:
            sid = source_slides[i].get("slide_id", f"slide_{i}")
            oid = source_slides[i].get("outline_node_id", "unknown")
            slides.append({
                "source_slide_id": sid,
                "source_outline_node_id": oid,
                "page_type": "",
                "rationale": "",
                "slots": {},
                "content_warnings": [],
            })
            all_warnings.append(f"missing_result: slide {i} ({oid})")
        else:
            slides.append(result["slide_plan"])
            all_warnings.extend(result.get("warnings", []))
            total_llm_calls += result.get("llm_calls", 0)

    # Deduplicate warnings
    seen: set[str] = set()
    deduped: list[str] = []
    for w in all_warnings:
        if w not in seen:
            seen.add(w)
            deduped.append(w)

    return {
        "schema_version": "pin_and_paper_slide_plan.v1",
        "template_slug": "pin-and-paper",
        "deck_title": deck.get("deck_title", ""),
        "session_id": deck.get("session_id", ""),
        "slides": slides,
        "warnings": deduped,
        "_llm_calls": total_llm_calls,
    }
