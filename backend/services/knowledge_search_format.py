from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .retrieval.models import EvidenceChunk
from .retrieval.text_features import (
    core_anchors,
    infer_content_types,
    query_anchors,
    tokenize,
)


DETAIL_MIN = 250
DETAIL_TARGET = 420
DETAIL_MAX = 550
PREVIEW_MAX = 180

_TRAILING_CLOSERS = '"\'”’」』】》〉）)'
_SENTENCE_RE = re.compile(
    rf".+?(?:(?:[。！？!?；;]+|\.(?=\s|$))[{re.escape(_TRAILING_CLOSERS)}]*|$)",
    re.DOTALL,
)
_CLAUSE_RE = re.compile(r".+?(?:[，,、：:]|$)", re.DOTALL)
_SENTENCE_END_RE = re.compile(rf"[。！？!?；;][{re.escape(_TRAILING_CLOSERS)}]*$")
_DOCUMENT_LINE_RE = re.compile(r"(?:\.pdf\s*$|[（(]?OCR[）)]?\s*(?:\.pdf)?\s*$)", re.IGNORECASE)
_CHAPTER_RE = re.compile(r"^第\s*[一二三四五六七八九十百零〇0-9]+\s*[编篇章部]")
_SECTION_RE = re.compile(r"^第\s*[一二三四五六七八九十百零〇0-9]+\s*节")
_MAJOR_HEADING_RE = re.compile(r"^[一二三四五六七八九十百]+、\s*\S+")
_PAREN_HEADING_RE = re.compile(r"^[（(][一二三四五六七八九十百]+[）)]\s*\S+")
_BRACKET_HEADING_RE = re.compile(r"^【[^】]{1,40}】$")
_LIST_RE = re.compile(
    r"^\s*(?:(?:\d{1,2}\.(?!\d))|(?:\d{1,2}[、)])|(?:[（(]\d{1,2}[）)])|[①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳]|[•·▪])\s*(.+)$"
)
_TABLE_RE = re.compile(r"^表\s*\d+(?:[-—–.]\d+)*", re.IGNORECASE)
_REFERENCE_HEADING_RE = re.compile(r"^(?:主要)?参考文献|^[^。！？]{0,20}参考文献$")
_REFERENCE_ENTRY_RE = re.compile(
    r"^(?:\[?\d+\]?[.、)]\s*)?[A-ZÀ-ÖØ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'’-]+(?:\s+[A-Z][A-Za-z'’-]+)*.*(?:et\s+al\.|doi\s*:)",
    re.IGNORECASE,
)
_IMAGE_ATTRIBUTION_RE = re.compile(
    r"^(?:image|photo|photograph)\s+(?:provided|courtesy|supplied|used)",
    re.IGNORECASE,
)
_CAPTION_RE = re.compile(r"^(?:该图像|此图像|这张照片|该照片|图\s*\d+(?:[-—–.]\d+)*)")
_OBJECTIVE_RE = re.compile(r"^(?:掌握|熟悉|了解|学习目标|教学目标)[：:]")
_CONTENT_HEADING_TERMS = {
    "定义",
    "概念",
    "病因",
    "危险因素",
    "病因和发病机制",
    "发病机制",
    "病理变化",
    "组织病理学",
    "临床表现",
    "症状和体征",
    "检查",
    "诊断",
    "鉴别诊断",
    "治疗",
    "治疗原则",
    "预后",
    "并发症",
    "禁忌证",
}
_GENERIC_TERMS = {
    "一下",
    "了解",
    "什么",
    "介绍",
    "内容",
    "哪些",
    "如何",
    "怎么",
    "是否",
    "有关",
    "相关",
    "知识",
    "请问",
}
_PROSE_RESTART_RE = re.compile(
    r"^(?:本病|患者|临床|治疗|诊断|鉴别|检查|研究|口腔|牙周|牙齿|因此|然而|但是|若|如果|由于)"
)
_TITLE_SEPARATOR_RE = re.compile(r"\s*[｜|]\s*")
_TITLE_GENERIC_PREFIXES = {"注意", "提示", "说明", "例如", "包括", "小结"}


@dataclass
class _Line:
    kind: str
    text: str
    level: int = 0
    preferred: bool = True


@dataclass(frozen=True)
class _Block:
    kind: str
    text: str = ""
    items: tuple[str, ...] = ()
    level: int = 0
    preferred: bool = True
    origin: int = -1


@dataclass(frozen=True)
class _Section:
    blocks: tuple[_Block, ...]
    label: str
    level: int
    parents: tuple[str, ...]
    order: int


@dataclass(frozen=True)
class _QueryContext:
    intents: frozenset[str]
    anchors: tuple[str, ...]
    tokens: frozenset[str]


def _compact_inline(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").replace("\u3000", " ")).strip()


def _dedupe_key(value: str) -> str:
    return re.sub(r"\s+", "", value).casefold()


def _strip_document_markers(value: Any) -> str:
    text = _compact_inline(value)
    text = re.sub(r"^[（(]已压缩[）)]\s*", "", text)
    text = re.sub(r"\s*[（(]?OCR[）)]?\s*(?=\.pdf|$)", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\.pdf\s*$", "", text, flags=re.IGNORECASE)
    return text.strip(" -/｜|")


def _book_label(value: Any) -> str:
    text = _strip_document_markers(value)
    match = re.search(r"《([^》]{2,40})》", text)
    if match:
        return match.group(1).strip()
    if len(text) <= 40 and not _looks_like_document_line(text):
        return text
    return ""


def _looks_like_document_line(value: str) -> bool:
    text = _compact_inline(value)
    if not text or len(text) > 240:
        return False
    return bool(
        _DOCUMENT_LINE_RE.search(text)
        or ("已压缩" in text and ("《" in text or "OCR" in text.upper()))
    )


def _display_heading(value: Any) -> str:
    text = _strip_document_markers(value)
    if _BRACKET_HEADING_RE.fullmatch(text):
        text = text[1:-1].strip()
    text = re.sub(r"^第\s*[一二三四五六七八九十百零〇0-9]+\s*[编篇章部节]\s*", "", text)
    text = re.sub(r"^[一二三四五六七八九十百]+、\s*", "", text)
    text = re.sub(r"^[（(][一二三四五六七八九十百0-9]+[）)]\s*", "", text)
    text = re.sub(r"^\d{1,2}[.、)]\s*", "", text)
    return text.strip(" -/｜|")


def _title_fragments(value: Any) -> list[str]:
    raw = _strip_document_markers(value)
    if not raw:
        return []
    fragments: list[str] = []
    for part in _TITLE_SEPARATOR_RE.split(raw):
        display = _display_heading(part)
        if not display:
            continue
        if "：" in display or ":" in display:
            prefix = re.split(r"[：:]", display, maxsplit=1)[0].strip()
            if (
                1 < len(prefix) <= 24
                and prefix not in _TITLE_GENERIC_PREFIXES
                and not re.search(r"[。！？!?；;，,]", prefix)
            ):
                display = prefix
        if len(display) > 36 or re.search(r"[。！？!?；;]", display):
            continue
        if display not in fragments:
            fragments.append(display)
    return fragments


def _heading_level(text: str, heading_hints: set[str]) -> int | None:
    if _CHAPTER_RE.match(text):
        return 1
    if _SECTION_RE.match(text):
        return 2
    if _MAJOR_HEADING_RE.match(text):
        return 3
    if _BRACKET_HEADING_RE.fullmatch(text) or _PAREN_HEADING_RE.match(text):
        return 4

    key = _dedupe_key(text)
    if key in heading_hints:
        return 4

    display = _display_heading(text)
    if display in _CONTENT_HEADING_TERMS:
        return 4
    if (
        1 < len(text) <= 24
        and not re.search(r"[。！？!?；;，,：:]", text)
        and infer_content_types("", title=text) != ["general"]
    ):
        return 4
    return None


def _list_item(text: str) -> str | None:
    match = _LIST_RE.match(text)
    if not match:
        return None
    item = _compact_inline(match.group(1))
    return item or None


def _ends_sentence(text: str) -> bool:
    return bool(_SENTENCE_END_RE.search(text.rstrip()))


def _soft_join(left: str, right: str) -> str:
    if not left:
        return right
    if not right:
        return left
    separator = " " if left[-1].isascii() and left[-1].isalnum() and right[0].isascii() and right[0].isalnum() else ""
    return left.rstrip() + separator + right.lstrip()


def _is_reference_heading(text: str) -> bool:
    return bool(_REFERENCE_HEADING_RE.match(_display_heading(text)))


def _is_reference_entry(text: str) -> bool:
    compact = _compact_inline(text)
    return bool(
        _REFERENCE_ENTRY_RE.match(compact)
        or re.search(r"\bdoi\s*:\s*10\.\d+", compact, flags=re.IGNORECASE)
    )


def _is_caption(text: str) -> bool:
    compact = _compact_inline(text)
    return bool(_IMAGE_ATTRIBUTION_RE.match(compact) or _CAPTION_RE.match(compact))


def _is_objective(text: str) -> bool:
    compact = _compact_inline(text)
    return bool(_OBJECTIVE_RE.match(compact) or compact.startswith(">>内容提要"))


def _looks_like_prose_restart(text: str) -> bool:
    compact = _compact_inline(text)
    return bool(len(compact) >= 36 and _ends_sentence(compact) and _PROSE_RESTART_RE.match(compact))


def _heading_hints(chunk: EvidenceChunk) -> set[str]:
    values = chunk.metadata.get("heading_path")
    if not isinstance(values, list):
        return set()
    return {_dedupe_key(_compact_inline(value)) for value in values if _compact_inline(value)}


def _logical_lines(chunk: EvidenceChunk) -> list[_Line]:
    raw = str(chunk.text or "").replace("\r\n", "\n").replace("\r", "\n")
    hints = _heading_hints(chunk)
    lines: list[_Line] = []
    last_kept_key = ""
    reference_mode = False
    table_mode = False

    for raw_line in raw.split("\n"):
        text = _compact_inline(raw_line)
        if not text or _looks_like_document_line(text):
            continue

        key = _dedupe_key(text)
        if key == last_kept_key:
            continue
        last_kept_key = key

        level = _heading_level(text, hints)
        item = _list_item(text)
        preferred = True
        kind = "paragraph"

        if level is not None:
            kind = "heading"
            reference_mode = _is_reference_heading(text)
            table_mode = False
            preferred = not reference_mode
        elif _TABLE_RE.match(text):
            table_mode = True
            kind = "paragraph"
            preferred = False
        elif table_mode and not _looks_like_prose_restart(text):
            preferred = False
        else:
            if table_mode:
                table_mode = False
            if item is not None:
                kind = "list"
                text = item
            if reference_mode or _is_reference_entry(text) or _is_caption(text) or _is_objective(text):
                preferred = False

        current = _Line(kind=kind, text=text, level=level or 0, preferred=preferred)
        if lines and lines[-1].kind == current.kind and lines[-1].preferred == current.preferred:
            previous = lines[-1]
            if current.kind == "paragraph" and not _ends_sentence(previous.text):
                previous.text = _soft_join(previous.text, current.text)
                continue
        if lines and lines[-1].kind == "list" and current.kind == "paragraph" and not _ends_sentence(lines[-1].text):
            lines[-1].text = _soft_join(lines[-1].text, current.text)
            lines[-1].preferred = lines[-1].preferred and current.preferred
            continue
        lines.append(current)
    return lines


def _parse_blocks(chunk: EvidenceChunk) -> list[_Block]:
    blocks: list[_Block] = []
    origin = 0
    for line in _logical_lines(chunk):
        if line.kind == "list":
            if blocks and blocks[-1].kind == "list" and blocks[-1].preferred == line.preferred:
                previous = blocks.pop()
                blocks.append(
                    _Block(
                        kind="list",
                        items=(*previous.items, line.text),
                        preferred=line.preferred,
                        origin=previous.origin,
                    )
                )
                continue
            blocks.append(_Block(kind="list", items=(line.text,), preferred=line.preferred, origin=origin))
        else:
            blocks.append(
                _Block(
                    kind=line.kind,
                    text=line.text,
                    level=line.level,
                    preferred=line.preferred,
                    origin=origin,
                )
            )
        origin += 1
    return blocks


def _parent_headings(blocks: list[_Block], index: int, level: int) -> tuple[str, ...]:
    found: dict[int, str] = {}
    for previous in reversed(blocks[:index]):
        if previous.kind != "heading" or previous.level >= level or previous.level in found:
            continue
        found[previous.level] = previous.text
    return tuple(found[key] for key in sorted(found))


def _section_candidates(blocks: list[_Block]) -> list[_Section]:
    if not blocks:
        return []
    headings = [index for index, block in enumerate(blocks) if block.kind == "heading"]
    sections: list[_Section] = []
    order = 0

    if not headings:
        return [_Section(tuple(blocks), "", 0, (), 0)]
    if headings[0] > 0:
        sections.append(_Section(tuple(blocks[: headings[0]]), "", 0, (), order))
        order += 1

    for position, start in enumerate(headings):
        heading = blocks[start]
        end = len(blocks)
        for candidate in headings[position + 1 :]:
            if blocks[candidate].level <= heading.level:
                end = candidate
                break
        sections.append(
            _Section(
                blocks=tuple(blocks[start:end]),
                label=heading.text,
                level=heading.level,
                parents=_parent_headings(blocks, start, heading.level),
                order=order,
            )
        )
        order += 1
    return sections


def _query_context(query: str) -> _QueryContext:
    intents = frozenset(label for label in infer_content_types(query) if label != "general")
    candidates = [*core_anchors(query), *query_anchors(query)]
    anchors: list[str] = []
    for candidate in candidates:
        term = _compact_inline(candidate).lower()
        if len(term) < 2 or term in _GENERIC_TERMS or term in anchors:
            continue
        anchors.append(term)
    tokens = frozenset(token for token in tokenize(query) if len(token) >= 2)
    return _QueryContext(intents=intents, anchors=tuple(anchors[:32]), tokens=tokens)


def _metadata_content_types(chunk: EvidenceChunk) -> set[str]:
    values: list[Any] = []
    for key in ("content_type", "content_types"):
        value = chunk.metadata.get(key)
        if isinstance(value, list):
            values.extend(value)
        elif value:
            values.append(value)
    return {str(value).strip() for value in values if str(value).strip()}


def _block_text(block: _Block) -> str:
    if block.kind == "list":
        return "\n".join(block.items)
    return block.text


def _semantic_score(text: str, title: str, context: _QueryContext) -> float:
    haystack = f"{title}\n{text}".lower()
    labels = set(infer_content_types(text, title=title)) - {"general"}
    score = 0.0
    score += 28.0 * len(labels & context.intents)
    for anchor in context.anchors:
        if anchor in title.lower():
            score += 8.0
        elif anchor in haystack:
            score += 2.5
    if context.tokens:
        overlap = context.tokens & set(tokenize(haystack))
        score += min(len(overlap), 16) * 0.18
    return score


def _section_score(section: _Section, chunk: EvidenceChunk, context: _QueryContext) -> tuple[float, int, int]:
    preferred = [block for block in section.blocks if block.preferred]
    content = "\n".join(_block_text(block) for block in preferred if block.kind != "heading")
    heading = "\n".join((*section.parents, section.label))
    score = _semantic_score(content, heading, context)
    metadata_types = _metadata_content_types(chunk)
    score += 7.0 * len(metadata_types & context.intents)
    if not any(block.kind != "heading" for block in preferred):
        score -= 60.0
    score += section.level * 0.35
    return score, section.level, -section.order


def _select_section(sections: list[_Section], chunk: EvidenceChunk, context: _QueryContext) -> _Section | None:
    if not sections:
        return None
    return max(sections, key=lambda section: _section_score(section, chunk, context))


def _split_sentences(text: str) -> list[str]:
    return [
        _compact_inline(match.group(0))
        for match in _SENTENCE_RE.finditer(text)
        if _compact_inline(match.group(0))
    ]


def _group_fragments(fragments: list[str], limit: int) -> list[str]:
    groups: list[str] = []
    pending = ""
    for fragment in fragments:
        candidate = pending + fragment
        if pending and len(candidate) > limit:
            groups.append(pending)
            pending = fragment
        else:
            pending = candidate
    if pending:
        groups.append(pending)
    return groups


def _split_long_paragraph(block: _Block) -> list[_Block]:
    if len(block.text) <= DETAIL_MAX:
        return [block]
    sentences = _split_sentences(block.text)
    if len(sentences) <= 1:
        clauses = [
            _compact_inline(match.group(0))
            for match in _CLAUSE_RE.finditer(block.text)
            if _compact_inline(match.group(0))
        ]
        if len(clauses) <= 1:
            return [block]
        sentences = clauses
    return [
        _Block(
            kind="paragraph",
            text=text,
            preferred=block.preferred,
            origin=block.origin,
        )
        for text in _group_fragments(sentences, DETAIL_MAX)
    ]


def _split_long_list(block: _Block) -> list[_Block]:
    if len(_block_text(block)) <= DETAIL_MAX:
        return [block]
    groups: list[list[str]] = []
    pending: list[str] = []
    pending_length = 0
    for item in block.items:
        extra = len(item) + (1 if pending else 0)
        if pending and pending_length + extra > DETAIL_MAX:
            groups.append(pending)
            pending = [item]
            pending_length = len(item)
        else:
            pending.append(item)
            pending_length += extra
    if pending:
        groups.append(pending)
    return [
        _Block(kind="list", items=tuple(items), preferred=block.preferred, origin=block.origin)
        for items in groups
    ]


def _expand_blocks(blocks: list[_Block]) -> list[_Block]:
    expanded: list[_Block] = []
    for block in blocks:
        if block.kind == "paragraph":
            expanded.extend(_split_long_paragraph(block))
        elif block.kind == "list":
            expanded.extend(_split_long_list(block))
        else:
            expanded.append(block)
    return expanded


def _plain_length(blocks: list[_Block]) -> int:
    return len(_blocks_to_plain(blocks))


def _individual_block_score(block: _Block, section: _Section, context: _QueryContext) -> float:
    if block.kind == "heading":
        return _semantic_score("", block.text, context)
    return _semantic_score(_block_text(block), section.label, context)


def _merge_split_blocks(blocks: list[_Block]) -> list[_Block]:
    merged: list[_Block] = []
    for block in blocks:
        if merged and merged[-1].kind == block.kind and merged[-1].origin == block.origin:
            previous = merged.pop()
            if block.kind == "paragraph":
                merged.append(
                    _Block(
                        kind="paragraph",
                        text=previous.text + block.text,
                        preferred=True,
                        origin=block.origin,
                    )
                )
            elif block.kind == "list":
                merged.append(
                    _Block(
                        kind="list",
                        items=(*previous.items, *block.items),
                        preferred=True,
                        origin=block.origin,
                    )
                )
            continue
        merged.append(block)
    while merged and merged[-1].kind == "heading":
        merged.pop()
    return merged


def _fit_section(section: _Section, context: _QueryContext) -> list[_Block]:
    preferred = [block for block in section.blocks if block.preferred]
    if not any(block.kind != "heading" for block in preferred):
        preferred = list(section.blocks)
    if not preferred:
        return []
    if _plain_length(preferred) <= DETAIL_MAX:
        return preferred

    expanded = _expand_blocks(preferred)
    content_indices = [index for index, block in enumerate(expanded) if block.kind != "heading"]
    if not content_indices:
        return expanded[:1]
    scores = {
        index: _individual_block_score(expanded[index], section, context)
        for index in range(len(expanded))
    }
    seed = max(content_indices, key=lambda index: (scores[index], -index))
    selected = {seed}
    left = seed
    right = seed
    seed_score = scores[seed]

    while True:
        current = [expanded[index] for index in sorted(selected)]
        current_length = _plain_length(current)
        options: list[tuple[float, int, int]] = []
        for index in (left - 1, right + 1):
            if index < 0 or index >= len(expanded):
                continue
            candidate_blocks = [expanded[item] for item in sorted((*selected, index))]
            if _plain_length(candidate_blocks) > DETAIL_MAX:
                continue
            same_origin = expanded[index].origin in {expanded[item].origin for item in selected}
            candidate_score = scores[index]
            meaningful = same_origin or candidate_score > 0
            if current_length < DETAIL_MIN:
                if not meaningful:
                    continue
            elif not same_origin and candidate_score < max(1.0, seed_score * 0.12):
                continue
            options.append((candidate_score + (4.0 if same_origin else 0.0), -abs(index - seed), index))
        if not options:
            break
        _, _, chosen = max(options)
        selected.add(chosen)
        left = min(left, chosen)
        right = max(right, chosen)
        if _plain_length([expanded[index] for index in sorted(selected)]) >= DETAIL_TARGET:
            remaining_relevant = any(
                scores[index] >= max(1.0, seed_score * 0.12)
                for index in (left - 1, right + 1)
                if 0 <= index < len(expanded)
            )
            if not remaining_relevant:
                break

    first_heading = next((block for block in expanded if block.kind == "heading"), None)
    chosen = [expanded[index] for index in sorted(selected)]
    if first_heading and first_heading not in chosen:
        with_heading = [first_heading, *chosen]
        if _plain_length(with_heading) <= DETAIL_MAX:
            chosen = with_heading
    return _merge_split_blocks(chosen)


def _blocks_to_plain(blocks: list[_Block]) -> str:
    values: list[str] = []
    for block in blocks:
        value = _block_text(block).strip()
        if value:
            values.append(value)
    return "\n\n".join(values).strip()


def _preview_from_blocks(blocks: list[_Block], section: _Section, context: _QueryContext) -> str:
    content = [block for block in _expand_blocks(blocks) if block.kind != "heading"]
    if not content:
        return _display_heading(section.label)
    best = max(content, key=lambda block: _individual_block_score(block, section, context))
    text = _block_text(best).replace("\n", " ").strip()
    if len(text) <= PREVIEW_MAX:
        return text

    sentences = [sentence for sentence in _split_sentences(text) if len(sentence) <= PREVIEW_MAX]
    if sentences:
        return max(sentences, key=lambda sentence: _semantic_score(sentence, section.label, context))
    clauses = [
        _compact_inline(match.group(0))
        for match in _CLAUSE_RE.finditer(text)
        if _compact_inline(match.group(0)) and len(_compact_inline(match.group(0))) <= PREVIEW_MAX
    ]
    if clauses:
        return max(clauses, key=lambda clause: _semantic_score(clause, section.label, context))
    return text[: PREVIEW_MAX - 3].rstrip() + "..."


def _topic_label(chunk: EvidenceChunk, section: _Section) -> str:
    structural_candidates: list[Any] = []
    heading_path = chunk.metadata.get("heading_path")
    if isinstance(heading_path, list):
        structural_candidates.extend(heading_path)
    structural_candidates.extend(section.parents)

    subject = _book_label(chunk.metadata.get("subject"))

    def collect(candidates: list[Any], *, skip_chapter: bool) -> list[str]:
        topics: list[str] = []
        for candidate in candidates:
            if _looks_like_document_line(_compact_inline(candidate)):
                continue
            for display in _title_fragments(candidate):
                if not display or display in _CONTENT_HEADING_TERMS or display == subject:
                    continue
                if skip_chapter and display == _display_heading(chunk.metadata.get("chapter")):
                    continue
                if display not in topics:
                    topics.append(display)
        return topics

    structural_topics = collect(structural_candidates, skip_chapter=True)
    if structural_topics:
        return structural_topics[-1]
    fallback_topics = collect([chunk.title, chunk.path], skip_chapter=False)
    return fallback_topics[-1] if fallback_topics else ""


def _fallback_content_label(chunk: EvidenceChunk) -> str:
    for candidate in (chunk.title, *reversed(chunk.metadata.get("heading_path") or [])):
        for fragment in reversed(_title_fragments(candidate)):
            labels = set(infer_content_types("", title=fragment)) - {"general"}
            if fragment in _CONTENT_HEADING_TERMS or labels:
                return fragment
    return ""


def _result_title(chunk: EvidenceChunk, section: _Section) -> str:
    topic = _topic_label(chunk, section)
    section_fragments = _title_fragments(section.label)
    content = section_fragments[-1] if section_fragments else ""
    if not content:
        content = _fallback_content_label(chunk)
    content_types = set(infer_content_types("", title=content)) - {"general"}
    if content == topic:
        content = ""
    if topic and content and (content in _CONTENT_HEADING_TERMS or content_types):
        return f"{topic} · {content}"
    if topic:
        return topic
    if content:
        return content
    fallback = _title_fragments(chunk.title)
    return (fallback[-1] if fallback else "") or _display_heading(chunk.metadata.get("chapter")) or "相关知识片段"


def _subject_label(chunk: EvidenceChunk) -> str:
    subject = _book_label(chunk.metadata.get("subject"))
    if subject:
        return subject
    core_node_ids = chunk.metadata.get("core_node_ids")
    if isinstance(core_node_ids, list):
        for node_id in core_node_ids:
            match = re.match(r"kg://([^/]+)/", str(node_id))
            if match:
                return match.group(1).strip()
    return ""


def _public_blocks(blocks: list[_Block], title: str) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    title_labels = {_compact_inline(value) for value in title.split(" · ") if _compact_inline(value)}
    for block in blocks:
        if block.kind == "heading":
            heading = _display_heading(block.text)
            if not heading or (not output and heading in title_labels):
                continue
            output.append({"type": "heading", "text": heading})
        elif block.kind == "paragraph":
            output.append({"type": "paragraph", "text": block.text})
        elif block.kind == "list":
            output.append({"type": "list", "items": list(block.items)})
    return output


def _public_blocks_to_plain(blocks: list[dict[str, Any]]) -> str:
    values: list[str] = []
    for block in blocks:
        if block.get("type") == "list":
            value = "\n".join(str(item).strip() for item in block.get("items", []) if str(item).strip())
        else:
            value = str(block.get("text") or "").strip()
        if value:
            values.append(value)
    return "\n\n".join(values).strip()


def format_knowledge_chunk(chunk: EvidenceChunk, query: str) -> dict[str, Any]:
    """Build a page-specific, non-destructive reading view for a retrieved chunk."""

    parsed = _parse_blocks(chunk)
    sections = _section_candidates(parsed)
    context = _query_context(_compact_inline(query))
    section = _select_section(sections, chunk, context)
    if section is None:
        return {
            "title": _display_heading(chunk.title) or "相关知识片段",
            "subject": _subject_label(chunk),
            "chapter": _strip_document_markers(chunk.metadata.get("chapter")),
            "preview": "",
            "detail": "",
            "blocks": [],
        }

    selected = _fit_section(section, context)
    title = _result_title(chunk, section)
    blocks = _public_blocks(selected, title)
    detail = _public_blocks_to_plain(blocks)
    return {
        "title": title,
        "subject": _subject_label(chunk),
        "chapter": _strip_document_markers(chunk.metadata.get("chapter")),
        "preview": _preview_from_blocks(selected, section, context),
        "detail": detail,
        "blocks": blocks,
    }
