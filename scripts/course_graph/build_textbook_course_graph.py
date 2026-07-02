from __future__ import annotations

import argparse
import hashlib
import json
import os
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

from backend.services.env_loader import load_project_env
from backend.services.llm_client import LLMRequestError, chat_completion, is_llm_configured


DEFAULT_CONFIG = PROJECT_ROOT / "configs" / "course_graph.yaml"
OCR_TEXTBOOK_DIR = PROJECT_ROOT / "data" / "processed" / "ocr_textbooks"


CHAPTER_RE = re.compile(r"第[一二三四五六七八九十百0-9]+章\s*([^\n。；;]{0,34})")
PART_RE = re.compile(r"第[一二三四五六七八九十百0-9]+篇\s*([^\n。；;]{0,34})")
NOISE_LINE_RE = re.compile(
    r"(本资料仅用于学习交流|禁止用于商业用途|^\s*目录\s*$|^\s*目\s*$|^\s*录\s*$|人卫|ISBN|CIP|版权所有)"
)


def stable_id(prefix: str, *parts: object, length: int = 16) -> str:
    raw = "||".join(str(part) for part in parts)
    digest = hashlib.sha1(raw.encode("utf-8", errors="ignore")).hexdigest()[:length]
    return f"{prefix}:{digest}"


def slugify(value: str) -> str:
    text = re.sub(r"\s+", "_", str(value or "").strip().lower())
    text = re.sub(r"[^\w\u4e00-\u9fff.-]+", "_", text)
    return text.strip("_") or "item"


def compact(text: str, limit: int = 180) -> str:
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    return value if len(value) <= limit else value[: limit - 3].rstrip() + "..."


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
            count += 1
    return count


def read_json(path: Path, fallback: Any) -> Any:
    if not path.exists():
        return fallback
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return fallback


def iter_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                yield item


@dataclass
class Page:
    page: int
    text: str
    path: str = ""


@dataclass
class Chapter:
    id: str
    number: int
    title: str
    part_title: str
    start_page: int
    end_page: int
    pages: list[Page] = field(default_factory=list)

    @property
    def text(self) -> str:
        parts = []
        for page in self.pages:
            parts.append(f"\n\n<!-- page:{page.page} -->\n{page.text.strip()}")
        return "\n".join(parts).strip()


@dataclass
class ChapterChunk:
    index: int
    total: int
    start_page: int
    end_page: int
    pages: list[Page]
    label: str = ""

    @property
    def text(self) -> str:
        parts = []
        for page in self.pages:
            parts.append(f"\n\n<!-- page:{page.page} -->\n{page.text.strip()}")
        return "\n".join(parts).strip()

    @property
    def display_label(self) -> str:
        return self.label or f"{self.index:02d}"


def find_ocr_dir(textbook: dict[str, Any]) -> Path:
    needle = str(textbook.get("ocr_dir_match") or textbook.get("source_match") or "")
    for path in sorted(OCR_TEXTBOOK_DIR.iterdir()):
        if path.is_dir() and needle in path.name:
            return path
    raise FileNotFoundError(f"Could not find OCR dir matching: {needle}")


def load_pages(textbook: dict[str, Any]) -> list[Page]:
    ocr_dir = find_ocr_dir(textbook)
    start_page = int(textbook.get("start_page") or 1)
    end_page = int(textbook.get("end_page") or 99999)
    pages: list[Page] = []
    for path in sorted(ocr_dir.glob("page_*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        page_no = int(data.get("page") or 0)
        if page_no < start_page or page_no > end_page:
            continue
        text = normalize_page_text(str(data.get("text") or ""))
        if text:
            pages.append(Page(page=page_no, text=text, path=str(path.relative_to(PROJECT_ROOT))))
    return pages


def normalize_page_text(text: str) -> str:
    text = text.replace("\x00", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    lines = []
    for line in text.splitlines():
        value = line.strip()
        if not value:
            continue
        if NOISE_LINE_RE.search(value):
            continue
        lines.append(value)
    return "\n".join(lines).strip()


def parse_chapter_number(value: str) -> int:
    token = re.sub(r"^第|章.*$", "", value)
    if token.isdigit():
        return int(token)
    mapping = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    if token == "十":
        return 10
    if token.startswith("十"):
        return 10 + mapping.get(token[1:], 0)
    if token.endswith("十"):
        return mapping.get(token[:-1], 0) * 10
    if "十" in token:
        left, right = token.split("十", 1)
        return mapping.get(left, 0) * 10 + mapping.get(right, 0)
    return mapping.get(token, 0)


def clean_heading(value: str) -> str:
    text = re.sub(r"\s+", "", str(value or ""))
    text = text.strip(" ：:，,。；;、·. ")
    text = re.sub(r"[（(]?[已压缩].*$", "", text)
    text = re.sub(r"[fSβ（）()]+$", "", text)
    return text


def detect_chapters(pages: list[Page], textbook: dict[str, Any]) -> list[Chapter]:
    overrides = textbook.get("chapter_overrides") or []
    if overrides:
        chapters: list[Chapter] = []
        for item in overrides:
            start_page = int(item.get("start_page") or 0)
            end_page = int(item.get("end_page") or 0)
            selected_pages = [page for page in pages if start_page <= page.page <= end_page]
            if not selected_pages:
                continue
            number = int(item.get("number") or 0)
            chapter_id = f"{textbook['id']}:chapter_{number:02d}"
            chapters.append(
                Chapter(
                    id=chapter_id,
                    number=number,
                    title=clean_heading(item.get("title") or f"第{number}章"),
                    part_title=clean_heading(item.get("part_title") or ""),
                    start_page=selected_pages[0].page,
                    end_page=selected_pages[-1].page,
                    pages=selected_pages,
                )
            )
        return chapters

    current_part = ""
    candidates: dict[int, tuple[int, str, str]] = {}

    for page in pages:
        text = page.text[:1800]
        for match in PART_RE.finditer(text):
            current_part = clean_heading(match.group(0))
        for match in CHAPTER_RE.finditer(text):
            heading = clean_heading(match.group(0))
            number = parse_chapter_number(heading)
            title = clean_heading(heading)
            if not number or not title:
                continue
            existing = candidates.get(number)
            if existing is None or page.page < existing[0]:
                candidates[number] = (page.page, title, current_part)

    ordered = sorted(candidates.items(), key=lambda item: item[1][0])
    chapters: list[Chapter] = []
    for index, (number, (start_page, title, part_title)) in enumerate(ordered):
        next_start = ordered[index + 1][1][0] if index + 1 < len(ordered) else pages[-1].page + 1
        selected_pages = [page for page in pages if start_page <= page.page < next_start]
        if not selected_pages:
            continue
        chapter_id = f"{textbook['id']}:chapter_{number:02d}"
        chapters.append(
            Chapter(
                id=chapter_id,
                number=number,
                title=title,
                part_title=part_title,
                start_page=selected_pages[0].page,
                end_page=selected_pages[-1].page,
                pages=selected_pages,
            )
        )
    return chapters


def extract_first_json_value(text: str, opener: str, closer: str) -> str:
    value = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", value, flags=re.DOTALL | re.IGNORECASE)
    if fence:
        value = fence.group(1).strip()
    start = value.find(opener)
    if start < 0:
        return value

    depth = 0
    in_string = False
    escape = False
    for index in range(start, len(value)):
        char = value[index]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == opener:
            depth += 1
        elif char == closer:
            depth -= 1
            if depth == 0:
                return value[start : index + 1]
    return value[start:]


def extract_json_object(text: str) -> dict[str, Any]:
    return json.loads(extract_first_json_value(text, "{", "}"))


def extract_json_array(text: str) -> list[Any]:
    parsed = json.loads(extract_first_json_value(text, "[", "]"))
    return parsed if isinstance(parsed, list) else []


def repair_json_with_llm(raw_text: str, config: dict[str, Any]) -> dict[str, Any]:
    kg_cfg = config.get("course_graph") or {}
    llm_cfg = kg_cfg.get("llm") or {}
    model_env = kg_cfg.get("model_env") or {}
    system = "你是严格的 JSON 修复器。只修复语法错误，不改写医学内容。输出必须是合法 JSON，不要解释。"
    user = (
        "下面文本应当是一个 JSON 对象，但存在语法错误。请修复为合法 JSON，保留所有字段和原文含义：\n\n"
        + raw_text[:24000]
    )
    repaired = chat_completion(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        temperature=0.0,
        max_tokens=int(llm_cfg.get("max_tokens") or 4200),
        api_key_names=tuple(model_env.get("api_key_names") or ("QWEN_API_KEY", "DASHSCOPE_API_KEY")),
        base_url_names=tuple(model_env.get("base_url_names") or ("QWEN_BASE_URL", "DASHSCOPE_BASE_URL")),
        model_names=tuple(model_env.get("model_names") or ("QWEN_MODEL", "DASHSCOPE_MODEL")),
    )
    return extract_json_object(repaired)


def llm_json(
    messages: list[dict[str, str]],
    config: dict[str, Any],
    max_tokens: int | None = None,
    debug_path: Path | None = None,
) -> Any:
    kg_cfg = config.get("course_graph") or {}
    llm_cfg = kg_cfg.get("llm") or {}
    model_env = kg_cfg.get("model_env") or {}
    text = chat_completion(
        messages,
        temperature=float(llm_cfg.get("temperature") or 0.05),
        max_tokens=max_tokens or int(llm_cfg.get("max_tokens") or 4200),
        api_key_names=tuple(model_env.get("api_key_names") or ("QWEN_API_KEY", "DASHSCOPE_API_KEY")),
        base_url_names=tuple(model_env.get("base_url_names") or ("QWEN_BASE_URL", "DASHSCOPE_BASE_URL")),
        model_names=tuple(model_env.get("model_names") or ("QWEN_MODEL", "DASHSCOPE_MODEL")),
    )
    if debug_path:
        debug_path.parent.mkdir(parents=True, exist_ok=True)
        debug_path.with_suffix(".raw.txt").write_text(text, encoding="utf-8")
    try:
        stripped = text.strip()
        if stripped.startswith("["):
            return extract_json_array(text)
        return extract_json_object(text)
    except json.JSONDecodeError as first_error:
        if debug_path:
            debug_path.with_suffix(".parse_error.txt").write_text(str(first_error), encoding="utf-8")
        try:
            repaired = repair_json_with_llm(text, config)
            repaired["_raw_json_repaired"] = True
            if debug_path:
                write_json(debug_path.with_suffix(".repaired.json"), repaired)
            return repaired
        except Exception as repair_error:
            if debug_path:
                debug_path.with_suffix(".repair_error.txt").write_text(str(repair_error), encoding="utf-8")
            raise


def log_progress(message: str) -> None:
    print(f"[course_graph] {message}", file=sys.stderr, flush=True)


def chunk_chapter_pages(chapter: Chapter, max_chars: int) -> list[ChapterChunk]:
    chunks: list[ChapterChunk] = []
    current: list[Page] = []
    current_chars = 0
    budget = max(4000, max_chars)

    for page in chapter.pages:
        page_text = f"\n\n<!-- page:{page.page} -->\n{page.text.strip()}"
        page_chars = len(page_text)
        if current and current_chars + page_chars > budget:
            chunks.append(
                ChapterChunk(
                    index=len(chunks) + 1,
                    total=0,
                    start_page=current[0].page,
                    end_page=current[-1].page,
                    pages=current,
                )
            )
            current = []
            current_chars = 0
        current.append(page)
        current_chars += page_chars

    if current:
        chunks.append(
            ChapterChunk(
                index=len(chunks) + 1,
                total=0,
                start_page=current[0].page,
                end_page=current[-1].page,
                pages=current,
            )
        )

    total = len(chunks)
    return [
        ChapterChunk(
            index=chunk.index,
            total=total,
            start_page=chunk.start_page,
            end_page=chunk.end_page,
            pages=chunk.pages,
            label=chunk.label,
        )
        for chunk in chunks
    ]


def split_chunk_for_retry(chunk: ChapterChunk) -> list[ChapterChunk]:
    if len(chunk.pages) <= 1:
        return []
    midpoint = max(1, len(chunk.pages) // 2)
    page_groups = [chunk.pages[:midpoint], chunk.pages[midpoint:]]
    subchunks: list[ChapterChunk] = []
    base_label = chunk.display_label
    for suffix, pages in zip(("a", "b"), page_groups):
        if not pages:
            continue
        subchunks.append(
            ChapterChunk(
                index=chunk.index,
                total=chunk.total,
                start_page=pages[0].page,
                end_page=pages[-1].page,
                pages=pages,
                label=f"{base_label}{suffix}",
            )
        )
    return subchunks


def chunk_meta(chunk: ChapterChunk) -> dict[str, Any]:
    return {
        "index": chunk.index,
        "label": chunk.display_label,
        "total": chunk.total,
        "start_page": chunk.start_page,
        "end_page": chunk.end_page,
        "char_count": len(chunk.text),
    }


def clean_title_index(cards: list[dict[str, Any]]) -> list[str]:
    titles = []
    seen = set()
    for item in cards:
        if not isinstance(item, dict):
            continue
        title = clean_heading(item.get("title") or "")
        if not title or title in seen:
            continue
        titles.append(title)
        seen.add(title)
    return titles


def dedupe_strings(values: Iterable[Any], limit: int = 12, item_limit: int = 180) -> list[str]:
    result: list[str] = []
    seen = set()
    for value in values:
        text = compact(str(value or ""), item_limit)
        if not text or text in seen:
            continue
        result.append(text)
        seen.add(text)
        if len(result) >= limit:
            break
    return result


def draft_cards_from_chunks(chunk_results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    draft_cards: list[dict[str, Any]] = []
    for chunk_result in chunk_results:
        chunk = chunk_result.get("chunk") if isinstance(chunk_result.get("chunk"), dict) else {}
        chunk_index = int(chunk.get("index") or 0)
        for index, card in enumerate(chunk_result.get("knowledge_cards", []), 1):
            if not isinstance(card, dict):
                continue
            item = dict(card)
            item["_draft_id"] = f"chunk_{chunk_index:02d}_card_{index:02d}"
            item["_chunk_index"] = chunk_index
            item["_chunk_page_range"] = [chunk.get("start_page"), chunk.get("end_page")]
            item["title"] = clean_heading(item.get("title") or f"草稿卡片{len(draft_cards) + 1}")
            draft_cards.append(item)
    return draft_cards


def compact_draft_card_for_merge(card: dict[str, Any]) -> dict[str, Any]:
    pages = []
    for item in card.get("evidence", []) if isinstance(card.get("evidence"), list) else []:
        if not isinstance(item, dict):
            continue
        page = item.get("page")
        try:
            page = int(page)
        except (TypeError, ValueError):
            continue
        pages.append(page)
    return {
        "draft_id": card.get("_draft_id", ""),
        "title": card.get("title", ""),
        "card_type": card.get("card_type", ""),
        "chunk": card.get("_chunk_index"),
        "pages": sorted(set(pages))[:8],
        "summary": compact(card.get("summary", ""), 220),
        "core_points": as_string_list(card.get("core_points"))[:4],
    }


def build_cards_from_merge_plan(
    merge_plan: dict[str, Any],
    draft_cards: list[dict[str, Any]],
    textbook: dict[str, Any],
    chapter: Chapter,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    draft_by_id = {card.get("_draft_id"): card for card in draft_cards if card.get("_draft_id")}
    used_ids: set[str] = set()
    cards: list[dict[str, Any]] = []
    review_notes: list[dict[str, Any]] = []

    plan_items = merge_plan.get("card_plan") if isinstance(merge_plan.get("card_plan"), list) else []
    for plan_item in plan_items:
        if not isinstance(plan_item, dict):
            continue
        source_ids = [str(item).strip() for item in as_string_list(plan_item.get("source_draft_ids"))]
        sources = [draft_by_id[source_id] for source_id in source_ids if source_id in draft_by_id]
        if not sources:
            continue
        used_ids.update(card.get("_draft_id", "") for card in sources)
        title = clean_heading(plan_item.get("final_title") or plan_item.get("title") or sources[0].get("title") or "")
        summary = str(plan_item.get("summary") or "").strip()
        if not summary:
            summary = compact("；".join(compact(source.get("summary", ""), 110) for source in sources), 420)
        evidence = []
        for source in sources:
            if isinstance(source.get("evidence"), list):
                evidence.extend(source["evidence"])
        cards.append(
            {
                "card_type": str(plan_item.get("card_type") or sources[0].get("card_type") or "concept"),
                "title": title,
                "summary": summary,
                "core_points": dedupe_strings(
                    point for source in sources for point in as_string_list(source.get("core_points"))
                ),
                "prerequisites": dedupe_strings(
                    (item for source in sources for item in as_string_list(source.get("prerequisites"))), limit=8
                ),
                "common_confusions": dedupe_strings(
                    (item for source in sources for item in as_string_list(source.get("common_confusions"))), limit=8
                ),
                "evidence": evidence[:10],
                "review_status": str(plan_item.get("review_status") or "draft"),
            }
        )
        merge_reason = str(plan_item.get("merge_reason") or "").strip()
        if merge_reason and len(sources) > 1:
            review_notes.append(
                {
                    "type": "merge",
                    "note": f"长章节合并计划将 {', '.join(source_ids)} 合并为“{title}”：{merge_reason}",
                    "related_titles": [source.get("title", "") for source in sources],
                    "review_status": "pending",
                }
            )

    for draft in draft_cards:
        draft_id = str(draft.get("_draft_id") or "")
        if draft_id in used_ids:
            continue
        cards.append(
            {
                "card_type": str(draft.get("card_type") or "concept"),
                "title": clean_heading(draft.get("title") or "未命名知识卡片"),
                "summary": str(draft.get("summary") or "").strip(),
                "core_points": as_string_list(draft.get("core_points")),
                "prerequisites": as_string_list(draft.get("prerequisites")),
                "common_confusions": as_string_list(draft.get("common_confusions")),
                "evidence": draft.get("evidence") if isinstance(draft.get("evidence"), list) else [],
                "review_status": str(draft.get("review_status") or "draft"),
            }
        )
        review_notes.append(
            {
                "type": "merge",
                "note": f"模型合并计划未覆盖草稿卡片 {draft_id}，系统为保证章节完整性保留为独立卡片。",
                "related_titles": [draft.get("title", "")],
                "review_status": "pending",
            }
        )

    if not cards:
        raise RuntimeError(
            f"Chapter synthesis produced no cards for {textbook.get('title')} chapter {chapter.number}: {chapter.title}"
        )
    for item in merge_plan.get("review_notes", []) if isinstance(merge_plan.get("review_notes"), list) else []:
        if isinstance(item, dict):
            review_notes.append(item)
    return cards, review_notes


def call_chunk_llm(
    chapter: Chapter,
    chunk: ChapterChunk,
    textbook: dict[str, Any],
    config: dict[str, Any],
    prior_chunk_titles: list[str],
    debug_dir: Path | None = None,
    retry_depth: int = 0,
) -> dict[str, Any]:
    log_progress(
        f"chapter {chapter.number:02d} {chapter.title}: reading chunk {chunk.display_label}/{chunk.total} pages {chunk.start_page}-{chunk.end_page}"
    )
    system = (
        "你是口腔医学课程知识工程专家。你的任务是逐页阅读教材分块，抽取适合学习和入图的知识卡片。"
        "必须严格依据当前分块文本，不要编造分块外内容；证据必须引用页码标记。输出必须是合法 JSON。"
    )
    base = f"""
教材：{textbook.get('title')} {textbook.get('edition', '')}，作者/主编：{textbook.get('author', '')}
章节：第{chapter.number}章 {chapter.title}
分块：{chunk.index}/{chunk.total}，页码：{chunk.start_page}-{chunk.end_page}
"""
    prompt = base + f"""
请阅读当前分块，生成这个分块内的知识卡片。不要输出解释，只输出 JSON 对象：
{{"knowledge_cards": [{{...}}]}}

已在本章前面分块出现的卡片标题如下，用于避免机械重复；如果当前分块对旧卡片提供了新的关键证据或关键拓展，可以保留同名或近似标题，但 summary 要体现新增内容：
{json.dumps(prior_chunk_titles[-80:], ensure_ascii=False)}

每张卡片 schema：
{{
  "card_type": "concept|disease|etiology|mechanism|epidemiology|classification|diagnosis|treatment|procedure|material|anatomy|risk|prevention|prognosis|complication|exam|learning_objective",
  "title": "...",
  "summary": "...",
  "core_points": ["..."],
  "prerequisites": ["..."],
  "common_confusions": ["..."],
  "evidence": [{{"page": 0, "quote_or_paraphrase": "..."}}],
  "review_status": "draft"
}}

要求：
1. 卡片粒度要适合学习、复习和章节图谱连接，不要按关键词碎片化。
2. 优先抽取本分块真正承载教学意义的概念、疾病、机制、诊断、治疗、操作、材料、风险和考核点。
3. evidence.page 必须来自当前分块页码范围。
4. 如果 OCR 明显可疑，在 common_confusions 或 review_status 中体现，但不要凭空修正文义。

当前分块文本：
{chunk.text}
"""
    debug_name = f"chunk_{chunk.display_label}_cards.json"
    try:
        cards_result = llm_json(
            [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            config,
            max_tokens=5000,
            debug_path=(debug_dir / debug_name if debug_dir else None),
        )
    except (json.JSONDecodeError, LLMRequestError) as error:
        if debug_dir:
            error_suffix = "request_error" if isinstance(error, LLMRequestError) else "parse_error"
            (debug_dir / f"chunk_{chunk.display_label}_cards.{error_suffix}.txt").write_text(
                str(error), encoding="utf-8"
            )
        subchunks = split_chunk_for_retry(chunk)
        if not subchunks or retry_depth >= 3:
            raise
        failure_kind = "request failed" if isinstance(error, LLMRequestError) else "JSON was incomplete"
        log_progress(
            f"chapter {chapter.number:02d}: chunk {chunk.display_label} {failure_kind}; splitting into {len(subchunks)} smaller reading units"
        )
        combined_cards: list[dict[str, Any]] = []
        local_titles = list(prior_chunk_titles)
        child_results: list[dict[str, Any]] = []
        for subchunk in subchunks:
            child_result = call_chunk_llm(
                chapter,
                subchunk,
                textbook,
                config,
                local_titles,
                debug_dir,
                retry_depth=retry_depth + 1,
            )
            child_results.append(child_result)
            child_cards = child_result.get("knowledge_cards") if isinstance(child_result.get("knowledge_cards"), list) else []
            combined_cards.extend(card for card in child_cards if isinstance(card, dict))
            local_titles.extend(clean_title_index(child_cards))
        return {
            "chunk": chunk_meta(chunk),
            "split_retry": True,
            "retry_depth": retry_depth,
            "child_chunks": [result.get("chunk", {}) for result in child_results],
            "knowledge_cards": combined_cards,
        }
    if isinstance(cards_result, dict):
        cards = cards_result.get("knowledge_cards") or cards_result.get("cards") or []
    else:
        cards = cards_result if isinstance(cards_result, list) else []
    log_progress(f"chapter {chapter.number:02d}: chunk {chunk.display_label}/{chunk.total} produced {len(cards)} draft cards")
    return {
        "chunk": chunk_meta(chunk),
        "knowledge_cards": cards if isinstance(cards, list) else [],
    }


def call_chapter_synthesis_llm(
    chapter: Chapter,
    textbook: dict[str, Any],
    config: dict[str, Any],
    chunk_results: list[dict[str, Any]],
    debug_dir: Path | None = None,
) -> dict[str, Any]:
    log_progress(f"chapter {chapter.number:02d} {chapter.title}: synthesizing {len(chunk_results)} chunks")
    system = (
        "你是口腔医学课程知识工程专家。现在你要把同一章各分块抽取出的卡片做章节级整合。"
        "你的工作是去重、合并近义卡片、保留证据页码、生成章节大纲和章节内关系。"
        "不要引入分块结果之外的新医学事实。输出必须是合法 JSON。"
    )
    draft_cards = draft_cards_from_chunks(chunk_results)
    draft_index = [compact_draft_card_for_merge(card) for card in draft_cards]

    cards_prompt = f"""
教材：{textbook.get('title')} {textbook.get('edition', '')}，作者/主编：{textbook.get('author', '')}
章节：第{chapter.number}章 {chapter.title}
页码：{chapter.start_page}-{chapter.end_page}

下面是本章各分块抽取出的知识卡片草稿索引。请只做“合并计划”，不要重写完整证据，不要长篇输出。
输出 JSON 对象：
{{
  "card_plan": [
    {{
      "final_title": "...",
      "card_type": "concept|disease|etiology|mechanism|epidemiology|classification|diagnosis|treatment|procedure|material|anatomy|risk|prevention|prognosis|complication|exam|learning_objective",
      "source_draft_ids": ["chunk_01_card_01"],
      "summary": "用1-2句话概括合并后的学习卡片",
      "merge_reason": "为什么这些草稿应合并；若只有一个来源，写保留理由",
      "review_status": "draft"
    }}
  ],
  "review_notes": [
    {{"type": "ocr|merge|scope|conflict|uncertain", "note": "...", "related_titles": ["..."]}}
  ]
}}

合并要求：
1. source_draft_ids 必须来自下面索引；每个 draft_id 最好出现一次，除非你明确认为应舍弃并在 review_notes 说明。
2. 只合并同一知识点、近义标题或同一疾病/技术的不同页证据；上下游知识、并列疾病、鉴别对象不要合并。
3. final_title 要适合学生学习和图谱节点展示，避免过长。
4. summary 只基于草稿索引内容，不引入草稿外事实。
5. 输出保持简洁，证据、core_points、prerequisites、common_confusions 将由程序从原草稿汇总保留。

草稿卡片索引：
{json.dumps(draft_index, ensure_ascii=False)}
"""
    merge_plan = llm_json(
        [{"role": "system", "content": system}, {"role": "user", "content": cards_prompt}],
        config,
        max_tokens=3600,
        debug_path=(debug_dir / "chapter_synthesis_cards.json" if debug_dir else None),
    )
    if not isinstance(merge_plan, dict):
        return {"chapter": {}, "knowledge_cards": [], "relations": [], "review_notes": []}
    cards, merge_review_notes = build_cards_from_merge_plan(merge_plan, draft_cards, textbook, chapter)
    if debug_dir:
        write_json(debug_dir / "chapter_synthesis_cards.built.json", {"knowledge_cards": cards, "review_notes": merge_review_notes})
    log_progress(f"chapter {chapter.number:02d}: synthesized {len(cards)} chapter cards from {len(draft_cards)} draft cards")
    titles = clean_title_index(cards)

    outline_prompt = f"""
教材：{textbook.get('title')} {textbook.get('edition', '')}，作者/主编：{textbook.get('author', '')}
章节：第{chapter.number}章 {chapter.title}
页码：{chapter.start_page}-{chapter.end_page}

下面是已经整合后的本章卡片标题和摘要。请生成章节大纲，只输出 JSON 对象：
{{
  "title": "...",
  "learning_objectives": ["..."],
  "structure": [{{"heading": "...", "purpose": "..."}}],
  "chapter_summary": "..."
}}

本章整合卡片：
{json.dumps([compact_card_for_review(normalize_card(card, chapter, textbook)) for card in cards if isinstance(card, dict)], ensure_ascii=False)}
"""
    outline = llm_json(
        [{"role": "system", "content": system}, {"role": "user", "content": outline_prompt}],
        config,
        max_tokens=1800,
        debug_path=(debug_dir / "chapter_synthesis_outline.json" if debug_dir else None),
    )
    if not isinstance(outline, dict):
        outline = {}

    relations_prompt = f"""
教材：{textbook.get('title')} {textbook.get('edition', '')}，作者/主编：{textbook.get('author', '')}
章节：第{chapter.number}章 {chapter.title}
页码：{chapter.start_page}-{chapter.end_page}

本章整合后的知识卡片标题如下：
{json.dumps(titles, ensure_ascii=False)}

请只基于这些标题和整合卡片摘要，生成章节内关系与 review notes。输出 JSON 对象：
{{
  "relations": [
    {{
      "source": "卡片标题",
      "relation_type": "prerequisite_of|part_of|causes|leads_to|diagnosed_by|treated_by|differentiates_from|prevents|risk_factor_for|supports|related_to",
      "target": "卡片标题",
      "reason": "...",
      "evidence_page": 0,
      "review_status": "draft"
    }}
  ],
  "review_notes": [
    {{"type": "ocr|merge|scope|conflict|uncertain", "note": "...", "related_titles": ["..."]}}
  ]
}}

整合卡片摘要：
{json.dumps([compact_card_for_review(normalize_card(card, chapter, textbook)) for card in cards if isinstance(card, dict)], ensure_ascii=False)}
"""
    rel_review = llm_json(
        [{"role": "system", "content": system}, {"role": "user", "content": relations_prompt}],
        config,
        max_tokens=3000,
        debug_path=(debug_dir / "chapter_synthesis_relations.json" if debug_dir else None),
    )
    if not isinstance(rel_review, dict):
        rel_review = {"relations": [], "review_notes": []}
    log_progress(
        f"chapter {chapter.number:02d}: synthesized {len(rel_review.get('relations') or [])} chapter relations"
    )
    return {
        "chapter": outline,
        "knowledge_cards": cards,
        "relations": rel_review.get("relations") if isinstance(rel_review.get("relations"), list) else [],
        "review_notes": merge_review_notes
        + (rel_review.get("review_notes") if isinstance(rel_review.get("review_notes"), list) else []),
    }


def call_chapter_llm(
    chapter: Chapter,
    textbook: dict[str, Any],
    config: dict[str, Any],
    debug_dir: Path | None = None,
) -> dict[str, Any]:
    kg_cfg = config.get("course_graph") or {}
    max_chars = int((kg_cfg.get("processing") or {}).get("max_chars_per_llm_call") or 18000)
    chapter_text = chapter.text

    system = (
        "你是口腔医学课程知识工程专家。你的任务不是关键词匹配，而是像教研人员一样逐章阅读教材，"
        "把章节整理成可学习、可审核、可入图的知识卡片和关系。必须严格依据提供的章节文本，"
        "不要编造章节外知识；证据必须引用页码标记。输出必须是合法 JSON。"
    )
    chunks = chunk_chapter_pages(chapter, max_chars)
    if len(chunks) > 1:
        chunk_results = []
        prior_titles: list[str] = []
        for chunk in chunks:
            chunk_result = call_chunk_llm(chapter, chunk, textbook, config, prior_titles, debug_dir)
            chunk_results.append(chunk_result)
            prior_titles.extend(clean_title_index(chunk_result.get("knowledge_cards", [])))
        synthesis = call_chapter_synthesis_llm(chapter, textbook, config, chunk_results, debug_dir)
        synthesis["chunk_results"] = chunk_results
        return synthesis

    base = f"""
教材：{textbook.get('title')} {textbook.get('edition', '')}，作者/主编：{textbook.get('author', '')}
章节：第{chapter.number}章 {chapter.title}
页码：{chapter.start_page}-{chapter.end_page}
"""

    outline_prompt = base + f"""
请基于章节文本生成章节大纲。只输出 JSON 对象：
{{
  "title": "...",
  "learning_objectives": ["..."],
  "structure": [{{"heading": "...", "purpose": "..."}}],
  "chapter_summary": "..."
}}

章节文本：
{chapter_text}
"""
    log_progress(f"chapter {chapter.number:02d} {chapter.title}: reading whole chapter pages {chapter.start_page}-{chapter.end_page}")
    outline = llm_json(
        [{"role": "system", "content": system}, {"role": "user", "content": outline_prompt}],
        config,
        max_tokens=1800,
        debug_path=(debug_dir / "outline.json" if debug_dir else None),
    )

    cards_prompt = base + f"""
请基于章节文本生成知识卡片。不要输出解释，只输出 JSON 对象：
{{"knowledge_cards": [{{...}}]}}
每张卡片 schema：
{{
  "card_type": "concept|disease|etiology|mechanism|epidemiology|classification|diagnosis|treatment|procedure|material|anatomy|risk|prevention|prognosis|complication|exam|learning_objective",
  "title": "...",
  "summary": "...",
  "core_points": ["..."],
  "prerequisites": ["..."],
  "common_confusions": ["..."],
  "evidence": [{{"page": 0, "quote_or_paraphrase": "..."}}],
  "review_status": "draft"
}}
卡片粒度要适合学习和复习，不要按关键词碎片化。证据必须来自章节文本页码标记。

章节文本：
{chapter_text}
"""
    cards = llm_json(
        [{"role": "system", "content": system}, {"role": "user", "content": cards_prompt}],
        config,
        max_tokens=5000,
        debug_path=(debug_dir / "cards.json" if debug_dir else None),
    )
    if isinstance(cards, dict):
        cards = cards.get("knowledge_cards") or []
    log_progress(f"chapter {chapter.number:02d}: produced {len(cards) if isinstance(cards, list) else 0} cards")

    titles = [str(item.get("title")) for item in cards if isinstance(item, dict) and item.get("title")]
    relations_prompt = base + f"""
已生成的知识卡片标题如下：
{json.dumps(titles, ensure_ascii=False)}

请只基于这些卡片标题和章节文本，生成章节内关系与 review notes。输出 JSON 对象：
{{
  "relations": [
    {{
      "source": "卡片标题",
      "relation_type": "prerequisite_of|part_of|causes|leads_to|diagnosed_by|treated_by|differentiates_from|prevents|risk_factor_for|supports|related_to",
      "target": "卡片标题",
      "reason": "...",
      "evidence_page": 0,
      "review_status": "draft"
    }}
  ],
  "review_notes": [
    {{"type": "ocr|merge|scope|conflict|uncertain", "note": "...", "related_titles": ["..."]}}
  ]
}}

章节文本：
{chapter_text}
"""
    rel_review = llm_json(
        [{"role": "system", "content": system}, {"role": "user", "content": relations_prompt}],
        config,
        max_tokens=2600,
        debug_path=(debug_dir / "relations.json" if debug_dir else None),
    )
    if not isinstance(rel_review, dict):
        rel_review = {"relations": [], "review_notes": []}
    log_progress(f"chapter {chapter.number:02d}: produced {len(rel_review.get('relations') or [])} chapter relations")

    return {
        "chapter": outline if isinstance(outline, dict) else {},
        "knowledge_cards": cards if isinstance(cards, list) else [],
        "relations": rel_review.get("relations") or [],
        "review_notes": rel_review.get("review_notes") or [],
    }


def normalize_card(raw: dict[str, Any], chapter: Chapter, textbook: dict[str, Any]) -> dict[str, Any]:
    title = clean_heading(raw.get("title") or "未命名知识卡片")
    card_id = stable_id("card", textbook["id"], chapter.id, title)
    evidence = raw.get("evidence") if isinstance(raw.get("evidence"), list) else []
    return {
        "id": card_id,
        "textbook_id": textbook["id"],
        "textbook_title": textbook.get("title"),
        "chapter_id": chapter.id,
        "chapter_number": chapter.number,
        "chapter_title": chapter.title,
        "page_range": [chapter.start_page, chapter.end_page],
        "card_type": str(raw.get("card_type") or "concept"),
        "title": title,
        "summary": str(raw.get("summary") or "").strip(),
        "core_points": as_string_list(raw.get("core_points")),
        "prerequisites": as_string_list(raw.get("prerequisites")),
        "common_confusions": as_string_list(raw.get("common_confusions")),
        "evidence": normalize_evidence(evidence, chapter),
        "review_status": str(raw.get("review_status") or "draft"),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def normalize_relation(raw: dict[str, Any], chapter: Chapter, card_by_title: dict[str, str], textbook: dict[str, Any]) -> dict[str, Any]:
    source_title = clean_heading(raw.get("source") or "")
    target_title = clean_heading(raw.get("target") or "")
    relation_type = str(raw.get("relation_type") or "related_to").strip()
    return {
        "id": stable_id("rel", textbook["id"], chapter.id, source_title, relation_type, target_title),
        "textbook_id": textbook["id"],
        "chapter_id": chapter.id,
        "relation_scope": "chapter",
        "source": source_title,
        "source_card_id": card_by_title.get(source_title, ""),
        "relation_type": relation_type,
        "target": target_title,
        "target_card_id": card_by_title.get(target_title, ""),
        "reason": str(raw.get("reason") or "").strip(),
        "evidence_page": raw.get("evidence_page"),
        "review_status": str(raw.get("review_status") or "draft"),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def as_string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def normalize_evidence(items: list[Any], chapter: Chapter) -> list[dict[str, Any]]:
    evidence = []
    for item in items:
        if not isinstance(item, dict):
            continue
        page = item.get("page")
        try:
            page = int(page)
        except (TypeError, ValueError):
            page = None
        if page is not None and not (chapter.start_page <= page <= chapter.end_page):
            page = None
        evidence.append(
            {
                "page": page,
                "quote_or_paraphrase": compact(str(item.get("quote_or_paraphrase") or ""), 260),
            }
        )
    return evidence


def compact_card_for_review(card: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": card.get("id", ""),
        "title": card.get("title", ""),
        "card_type": card.get("card_type", ""),
        "chapter_number": card.get("chapter_number"),
        "chapter_title": card.get("chapter_title", ""),
        "page_range": card.get("page_range", []),
        "summary": compact(card.get("summary", ""), 180),
        "core_points": as_string_list(card.get("core_points"))[:3],
    }


def card_review_batches(cards: list[dict[str, Any]], max_chars: int) -> list[list[dict[str, Any]]]:
    batches: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_chars = 0
    budget = max(6000, max_chars)

    for card in cards:
        compact_card = compact_card_for_review(card)
        size = len(json.dumps(compact_card, ensure_ascii=False))
        if current and current_chars + size > budget:
            batches.append(current)
            current = []
            current_chars = 0
        current.append(compact_card)
        current_chars += size

    if current:
        batches.append(current)
    return batches


def call_cross_review_batch_llm(
    chapter: Chapter,
    textbook: dict[str, Any],
    config: dict[str, Any],
    current_cards: list[dict[str, Any]],
    prior_batch: list[dict[str, Any]],
    batch_index: int,
    batch_total: int,
) -> dict[str, Any]:
    system = (
        "你是口腔医学课程知识图谱审阅专家。你的任务是在每完成一章后，审阅本章卡片与已有章节卡片，"
        "判断哪些知识点可以跨章串联、哪些卡片可能需要合并、哪些内容需要人工复核。"
        "只依据给定卡片索引，不要使用外部知识补写事实。输出必须是合法 JSON。"
    )
    current_index = [compact_card_for_review(card) for card in current_cards]
    prompt = f"""
教材：{textbook.get('title')} {textbook.get('edition', '')}
当前章节：第{chapter.number}章 {chapter.title}，页码：{chapter.start_page}-{chapter.end_page}
已有卡片批次：{batch_index}/{batch_total}

当前章节卡片：
{json.dumps(current_index, ensure_ascii=False)}

此前章节卡片索引：
{json.dumps(prior_batch, ensure_ascii=False)}

请输出 JSON 对象：
{{
  "cross_relations": [
    {{
      "source_card_id": "card:...",
      "source_title": "...",
      "relation_type": "prerequisite_of|part_of|causes|leads_to|diagnosed_by|treated_by|differentiates_from|prevents|risk_factor_for|supports|related_to",
      "target_card_id": "card:...",
      "target_title": "...",
      "reason": "...",
      "evidence": "基于两个卡片的摘要、页码或核心点说明，不要编造原文外证据",
      "review_status": "draft"
    }}
  ],
  "merge_suggestions": [
    {{
      "current_card_id": "card:...",
      "current_title": "...",
      "existing_card_id": "card:...",
      "existing_title": "...",
      "decision": "merge|keep_separate|uncertain",
      "reason": "...",
      "review_status": "pending"
    }}
  ],
  "review_notes": [
    {{"type": "cross_link|merge|scope|conflict|uncertain", "note": "...", "related_card_ids": ["card:..."], "review_status": "pending"}}
  ]
}}

审阅要求：
1. 只连接确有课程逻辑价值的跨章关系；不要为了增加边数而泛泛 related_to。
2. 如果一个概念是后续诊断/治疗/操作的前置知识，优先使用 prerequisite_of。
3. 如果疾病进展、机制、诊断、治疗之间存在清晰方向，使用 causes/leads_to/diagnosed_by/treated_by/supports 等更具体关系。
4. 合并建议要谨慎：同名但教学层级不同的卡片可以 keep_separate 或 uncertain。
5. source_card_id 和 target_card_id 必须来自上面两个卡片索引。
"""
    result = llm_json([{"role": "system", "content": system}, {"role": "user", "content": prompt}], config, max_tokens=3200)
    if not isinstance(result, dict):
        return {"cross_relations": [], "merge_suggestions": [], "review_notes": []}
    return {
        "cross_relations": result.get("cross_relations") if isinstance(result.get("cross_relations"), list) else [],
        "merge_suggestions": result.get("merge_suggestions") if isinstance(result.get("merge_suggestions"), list) else [],
        "review_notes": result.get("review_notes") if isinstance(result.get("review_notes"), list) else [],
    }


def normalize_cross_relation(
    raw: dict[str, Any],
    chapter: Chapter,
    cards_by_id: dict[str, dict[str, Any]],
    cards_by_title: dict[str, list[dict[str, Any]]],
    textbook: dict[str, Any],
) -> dict[str, Any] | None:
    source_id = str(raw.get("source_card_id") or "").strip()
    target_id = str(raw.get("target_card_id") or "").strip()
    source_title = clean_heading(raw.get("source_title") or raw.get("source") or "")
    target_title = clean_heading(raw.get("target_title") or raw.get("target") or "")

    if source_id not in cards_by_id and source_title in cards_by_title and len(cards_by_title[source_title]) == 1:
        source_id = cards_by_title[source_title][0]["id"]
    if target_id not in cards_by_id and target_title in cards_by_title and len(cards_by_title[target_title]) == 1:
        target_id = cards_by_title[target_title][0]["id"]
    if source_id not in cards_by_id or target_id not in cards_by_id or source_id == target_id:
        return None

    source_card = cards_by_id[source_id]
    target_card = cards_by_id[target_id]
    relation_type = str(raw.get("relation_type") or "related_to").strip()
    return {
        "id": stable_id("cross_rel", textbook["id"], source_id, relation_type, target_id),
        "textbook_id": textbook["id"],
        "chapter_id": chapter.id,
        "relation_scope": "cross_chapter",
        "source": source_card.get("title", source_title),
        "source_card_id": source_id,
        "source_chapter_number": source_card.get("chapter_number"),
        "relation_type": relation_type,
        "target": target_card.get("title", target_title),
        "target_card_id": target_id,
        "target_chapter_number": target_card.get("chapter_number"),
        "reason": str(raw.get("reason") or "").strip(),
        "evidence": str(raw.get("evidence") or "").strip(),
        "review_status": str(raw.get("review_status") or "draft"),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def normalize_merge_suggestion(
    raw: dict[str, Any],
    chapter: Chapter,
    cards_by_id: dict[str, dict[str, Any]],
    textbook: dict[str, Any],
) -> dict[str, Any] | None:
    current_id = str(raw.get("current_card_id") or "").strip()
    existing_id = str(raw.get("existing_card_id") or "").strip()
    if current_id not in cards_by_id or existing_id not in cards_by_id or current_id == existing_id:
        return None
    current_card = cards_by_id[current_id]
    existing_card = cards_by_id[existing_id]
    decision = str(raw.get("decision") or "uncertain").strip()
    return {
        "id": stable_id("review", textbook["id"], "merge", current_id, existing_id),
        "type": "llm_merge_suggestion",
        "textbook_id": textbook["id"],
        "chapter_id": chapter.id,
        "current_card_id": current_id,
        "current_title": current_card.get("title"),
        "current_chapter_number": current_card.get("chapter_number"),
        "existing_card_id": existing_id,
        "existing_title": existing_card.get("title"),
        "existing_chapter_number": existing_card.get("chapter_number"),
        "decision": decision if decision in {"merge", "keep_separate", "uncertain"} else "uncertain",
        "reason": str(raw.get("reason") or "").strip(),
        "review_status": str(raw.get("review_status") or "pending"),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def build_cross_chapter_review(
    chapter: Chapter,
    textbook: dict[str, Any],
    config: dict[str, Any],
    chapter_dir: Path,
    current_cards: list[dict[str, Any]],
    prior_cards: list[dict[str, Any]],
    force: bool,
) -> dict[str, Any]:
    review_path = chapter_dir / "cross_chapter_review.json"
    relation_path = chapter_dir / "cross_chapter_relations.jsonl"
    if review_path.exists() and relation_path.exists() and not force:
        return read_json(review_path, {"cross_relations": [], "merge_suggestions": [], "review_notes": []})

    if not prior_cards or not current_cards:
        review = {
            "cross_relations": [],
            "merge_suggestions": [],
            "review_notes": [],
            "batches_reviewed": 0,
        }
        write_json(review_path, review)
        write_jsonl(relation_path, [])
        return review

    log_progress(
        f"chapter {chapter.number:02d} {chapter.title}: cross-reviewing {len(current_cards)} current cards against {len(prior_cards)} prior cards"
    )
    kg_cfg = config.get("course_graph") or {}
    processing_cfg = kg_cfg.get("processing") or {}
    max_chars = int(processing_cfg.get("cross_review_max_chars_per_call") or processing_cfg.get("max_chars_per_llm_call") or 18000)
    prior_batches = card_review_batches(prior_cards, max_chars)
    all_cards = prior_cards + current_cards
    cards_by_id = {card["id"]: card for card in all_cards if card.get("id")}
    cards_by_title: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for card in all_cards:
        if card.get("title"):
            cards_by_title[card["title"]].append(card)

    raw_batches = []
    cross_relations: list[dict[str, Any]] = []
    merge_suggestions: list[dict[str, Any]] = []
    review_notes: list[dict[str, Any]] = []

    for index, batch in enumerate(prior_batches, 1):
        log_progress(f"chapter {chapter.number:02d}: cross-review batch {index}/{len(prior_batches)}")
        batch_result = call_cross_review_batch_llm(chapter, textbook, config, current_cards, batch, index, len(prior_batches))
        raw_batches.append({"batch": index, "prior_cards": len(batch), "result": batch_result})
        for rel in batch_result.get("cross_relations", []):
            if isinstance(rel, dict):
                normalized = normalize_cross_relation(rel, chapter, cards_by_id, cards_by_title, textbook)
                if normalized:
                    cross_relations.append(normalized)
        for suggestion in batch_result.get("merge_suggestions", []):
            if isinstance(suggestion, dict):
                normalized = normalize_merge_suggestion(suggestion, chapter, cards_by_id, textbook)
                if normalized:
                    merge_suggestions.append(normalized)
        for note in batch_result.get("review_notes", []):
            if isinstance(note, dict):
                review_notes.append(
                    {
                        "id": stable_id("review", textbook["id"], chapter.id, note.get("note", ""), len(review_notes)),
                        "type": str(note.get("type") or "cross_review_note"),
                        "textbook_id": textbook["id"],
                        "chapter_id": chapter.id,
                        "note": str(note.get("note") or "").strip(),
                        "related_card_ids": as_string_list(note.get("related_card_ids")),
                        "review_status": str(note.get("review_status") or "pending"),
                        "created_at": datetime.now(timezone.utc).isoformat(),
                    }
                )

    unique_relations = {rel["id"]: rel for rel in cross_relations}
    unique_merges = {item["id"]: item for item in merge_suggestions}
    review = {
        "cross_relations": list(unique_relations.values()),
        "merge_suggestions": list(unique_merges.values()),
        "review_notes": review_notes,
        "batches_reviewed": len(prior_batches),
        "raw_batch_count": len(raw_batches),
    }
    write_json(review_path, review)
    write_jsonl(relation_path, review["cross_relations"])
    write_json(chapter_dir / "cross_chapter_review_raw_batches.json", raw_batches)
    log_progress(
        f"chapter {chapter.number:02d}: cross-review produced {len(review['cross_relations'])} relations, {len(review['merge_suggestions'])} merge suggestions"
    )
    return review


def build_chapter_outputs(
    chapter: Chapter,
    textbook: dict[str, Any],
    config: dict[str, Any],
    out_dir: Path,
    prior_cards: list[dict[str, Any]],
    force: bool,
) -> dict[str, Any]:
    chapter_dir = out_dir / "chapter_runs" / textbook["id"] / f"chapter_{chapter.number:02d}"
    result_path = chapter_dir / "llm_result.json"
    chapter_dir.mkdir(parents=True, exist_ok=True)
    log_progress(f"start chapter {chapter.number:02d} {chapter.title} pages {chapter.start_page}-{chapter.end_page}")
    (chapter_dir / "chapter_text.md").write_text(
        f"# 第{chapter.number}章 {chapter.title}\n\n页码：{chapter.start_page}-{chapter.end_page}\n\n{chapter.text}\n",
        encoding="utf-8",
    )
    write_json(
        chapter_dir / "chapter_meta.json",
        {
            "id": chapter.id,
            "number": chapter.number,
            "title": chapter.title,
            "part_title": chapter.part_title,
            "start_page": chapter.start_page,
            "end_page": chapter.end_page,
            "page_count": len(chapter.pages),
            "char_count": len(chapter.text),
        },
    )

    if result_path.exists() and not force:
        result = read_json(result_path, {})
    else:
        result = call_chapter_llm(chapter, textbook, config, chapter_dir / "llm_debug")
        write_json(result_path, result)

    cards = [normalize_card(item, chapter, textbook) for item in result.get("knowledge_cards", []) if isinstance(item, dict)]
    card_by_title = {card["title"]: card["id"] for card in cards}
    relations = [
        normalize_relation(item, chapter, card_by_title, textbook)
        for item in result.get("relations", [])
        if isinstance(item, dict)
    ]
    review_notes = result.get("review_notes") if isinstance(result.get("review_notes"), list) else []
    outline = result.get("chapter") if isinstance(result.get("chapter"), dict) else {}

    write_json(chapter_dir / "chapter_outline.json", outline)
    write_jsonl(chapter_dir / "knowledge_cards.jsonl", cards)
    write_jsonl(chapter_dir / "chapter_relations.jsonl", relations)
    write_json(chapter_dir / "review_notes.json", review_notes)
    cross_review = build_cross_chapter_review(chapter, textbook, config, chapter_dir, cards, prior_cards, force)
    write_json(
        chapter_dir / "chapter_graph.json",
        {
            "nodes": [{"id": card["id"], "type": card["card_type"], "label": card["title"], "properties": card} for card in cards],
            "edges": [
                {
                    "id": rel["id"],
                    "source": rel["source_card_id"] or rel["source"],
                    "target": rel["target_card_id"] or rel["target"],
                    "type": rel["relation_type"],
                    "properties": rel,
                }
                for rel in relations
            ]
            + [
                {
                    "id": rel["id"],
                    "source": rel["source_card_id"],
                    "target": rel["target_card_id"],
                    "type": rel["relation_type"],
                    "properties": rel,
                }
                for rel in cross_review.get("cross_relations", [])
            ],
        },
    )
    return {
        "chapter_id": chapter.id,
        "chapter_number": chapter.number,
        "title": chapter.title,
        "cards": len(cards),
        "relations": len(relations),
        "cross_relations": len(cross_review.get("cross_relations", [])),
        "merge_suggestions": len(cross_review.get("merge_suggestions", [])),
        "cross_review_notes": len(cross_review.get("review_notes", [])),
        "review_notes": len(review_notes),
        "dir": str(chapter_dir.relative_to(PROJECT_ROOT)),
    }


def similarity_key(title: str) -> str:
    text = re.sub(r"[^\w\u4e00-\u9fff]+", "", title.lower())
    text = re.sub(r"(概述|定义|临床表现|诊断|治疗|原则|方法)$", "", text)
    return text


def build_master(textbook: dict[str, Any], out_dir: Path) -> dict[str, Any]:
    run_root = out_dir / "chapter_runs" / textbook["id"]
    master_dir = out_dir / "master"
    all_cards: list[dict[str, Any]] = []
    all_relations: list[dict[str, Any]] = []
    chapter_relations: list[dict[str, Any]] = []
    cross_relations: list[dict[str, Any]] = []
    review_queue: list[dict[str, Any]] = []

    for card_file in sorted(run_root.glob("chapter_*/knowledge_cards.jsonl")):
        all_cards.extend(iter_jsonl(card_file))
    for relation_file in sorted(run_root.glob("chapter_*/chapter_relations.jsonl")):
        chapter_relations.extend(iter_jsonl(relation_file))
    for relation_file in sorted(run_root.glob("chapter_*/cross_chapter_relations.jsonl")):
        cross_relations.extend(iter_jsonl(relation_file))
    all_relations = chapter_relations + cross_relations

    by_key: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for card in all_cards:
        by_key[similarity_key(card.get("title", ""))].append(card)

    for key, cards in by_key.items():
        if key and len(cards) > 1:
            review_queue.append(
                {
                    "type": "possible_merge",
                    "key": key,
                    "titles": sorted({card["title"] for card in cards}),
                    "card_ids": [card["id"] for card in cards],
                    "chapters": sorted({str(card.get("chapter_number")) for card in cards}),
                    "review_status": "pending",
                }
            )

    title_to_cards: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for card in all_cards:
        title_to_cards[card["title"]].append(card)
    for rel in all_relations:
        if not rel.get("source_card_id") and rel.get("source") in title_to_cards:
            review_queue.append({"type": "unresolved_relation_source", "relation_id": rel["id"], "source": rel.get("source"), "review_status": "pending"})
        if not rel.get("target_card_id") and rel.get("target") in title_to_cards:
            review_queue.append({"type": "unresolved_relation_target", "relation_id": rel["id"], "target": rel.get("target"), "review_status": "pending"})

    for review_file in sorted(run_root.glob("chapter_*/cross_chapter_review.json")):
        review = read_json(review_file, {})
        for item in review.get("merge_suggestions", []):
            if isinstance(item, dict):
                review_queue.append(item)
        for item in review.get("review_notes", []):
            if isinstance(item, dict):
                review_queue.append(item)
    for review_file in sorted(run_root.glob("chapter_*/review_notes.json")):
        chapter_notes = read_json(review_file, [])
        chapter_dir = review_file.parent.name
        for index, item in enumerate(chapter_notes if isinstance(chapter_notes, list) else []):
            if not isinstance(item, dict):
                continue
            review_queue.append(
                {
                    "id": stable_id("review", textbook["id"], chapter_dir, index, item.get("note", "")),
                    "type": item.get("type") or "chapter_review_note",
                    "textbook_id": textbook["id"],
                    "chapter_dir": chapter_dir,
                    "note": str(item.get("note") or "").strip(),
                    "related_titles": as_string_list(item.get("related_titles")),
                    "review_status": item.get("review_status") or "pending",
                }
            )

    master_dir.mkdir(parents=True, exist_ok=True)
    write_jsonl(master_dir / "cards.jsonl", all_cards)
    write_jsonl(master_dir / "relations.jsonl", all_relations)
    write_jsonl(master_dir / "chapter_relations.jsonl", chapter_relations)
    write_jsonl(master_dir / "cross_chapter_relations.jsonl", cross_relations)
    write_jsonl(master_dir / "review_queue.jsonl", review_queue)
    summary = {
        "ok": True,
        "textbook_id": textbook["id"],
        "textbook_title": textbook.get("title"),
        "cards": len(all_cards),
        "relations": len(all_relations),
        "chapter_relations": len(chapter_relations),
        "cross_chapter_relations": len(cross_relations),
        "review_items": len(review_queue),
        "card_types": Counter(card.get("card_type", "unknown") for card in all_cards),
        "relation_types": Counter(rel.get("relation_type", "unknown") for rel in all_relations),
        "chapters": len({card.get("chapter_id") for card in all_cards}),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    summary["card_types"] = dict(summary["card_types"])
    summary["relation_types"] = dict(summary["relation_types"])
    write_json(master_dir / "summary.json", summary)
    return summary


def build_textbook(textbook_id: str, config_path: Path, chapter_limit: int | None, force: bool) -> dict[str, Any]:
    load_project_env()
    config = load_yaml(config_path)
    textbooks = config.get("textbooks") or []
    textbook = next((item for item in textbooks if item.get("id") == textbook_id), None)
    if not textbook:
        raise ValueError(f"Textbook not found in config: {textbook_id}")

    kg_cfg = config.get("course_graph") or {}
    out_dir = PROJECT_ROOT / str(kg_cfg.get("output_dir") or "data/course_graph")
    if not is_llm_configured("COURSE_GRAPH_LLM_API_KEY", "QWEN_API_KEY", "DASHSCOPE_API_KEY", "OPENAI_API_KEY"):
        raise RuntimeError("No LLM API key configured. This workflow requires model-based chapter analysis.")

    pages = load_pages(textbook)
    chapters = detect_chapters(pages, textbook)
    min_chars = int((kg_cfg.get("processing") or {}).get("min_chapter_chars") or 900)
    chapters = [chapter for chapter in chapters if len(chapter.text) >= min_chars]
    if chapter_limit:
        chapters = chapters[:chapter_limit]

    textbook_dir = out_dir / "textbooks" / textbook["id"]
    textbook_dir.mkdir(parents=True, exist_ok=True)
    write_json(
        textbook_dir / "textbook_manifest.json",
        {
            **textbook,
            "page_count": len(pages),
            "chapters_detected": len(chapters),
            "chapters": [
                {
                    "id": chapter.id,
                    "number": chapter.number,
                    "title": chapter.title,
                    "part_title": chapter.part_title,
                    "start_page": chapter.start_page,
                    "end_page": chapter.end_page,
                    "char_count": len(chapter.text),
                }
                for chapter in chapters
            ],
        },
    )

    chapter_summaries = []
    accumulated_cards: list[dict[str, Any]] = []
    for chapter in chapters:
        chapter_summary = build_chapter_outputs(chapter, textbook, config, out_dir, accumulated_cards, force)
        chapter_summaries.append(chapter_summary)
        chapter_dir = out_dir / "chapter_runs" / textbook["id"] / f"chapter_{chapter.number:02d}"
        accumulated_cards.extend(iter_jsonl(chapter_dir / "knowledge_cards.jsonl"))
        build_master(textbook, out_dir)

    master_summary = build_master(textbook, out_dir)
    run_summary = {
        "ok": True,
        "textbook": textbook,
        "chapters_processed": len(chapter_summaries),
        "chapter_summaries": chapter_summaries,
        "master_summary": master_summary,
        "output_dir": str(out_dir.relative_to(PROJECT_ROOT)),
    }
    write_json(out_dir / "last_run_summary.json", run_summary)
    return run_summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Build textbook-by-chapter course knowledge cards and chapter graphs.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--textbook-id", default="operative_endodontics_zhouxuedong_5e")
    parser.add_argument("--chapter-limit", type=int, default=None)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    summary = build_textbook(args.textbook_id, args.config, args.chapter_limit, args.force)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
