from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from backend.services.decision.agent import run_evidence_search
from backend.services.retrieval.models import EvidenceChunk


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_existing_evidence_search_contract_remains_read_only() -> None:
    chunk = EvidenceChunk(
        id="chunk:test",
        text="牙周基础治疗包括口腔卫生指导、洁治和龈下清创。",
        source="测试知识库",
        source_type="textbook",
        title="牙周基础治疗",
        metadata={"subject": "牙周病学", "chapter": "牙周治疗"},
    )

    with patch("backend.services.decision.agent.search_knowledge_base", return_value=[chunk]) as search:
        result = run_evidence_search("牙周基础治疗", top_k=6)

    search.assert_called_once_with("牙周基础治疗", top_k=6)
    assert result["ok"] is True
    assert result["module"] == "evidence_search"
    assert result["query"] == "牙周基础治疗"
    assert result["count"] == 1
    assert result["results"][0]["id"] == "chunk:test"
    assert result["results"][0]["excerpt"]


def test_knowledge_search_page_uses_only_dedicated_endpoint() -> None:
    script = (PROJECT_ROOT / "frontend" / "js" / "evidence.js").read_text(encoding="utf-8")
    page = (PROJECT_ROOT / "frontend" / "pages" / "evidence.html").read_text(encoding="utf-8")

    assert "/api/knowledge/search" in script
    assert "/api/evidence/search" not in script
    assert "/api/knowledge/catalog" not in script
    assert "/api/knowledge/subject" not in script
    assert "/api/knowledge/entry" not in script
    assert "/api/knowledge/view" not in script
    assert script.count("apiPost(") == 1
    assert "oralsage.knowledgeSearch.cache.v3" in script
    assert "oralsage.knowledgeSearch.cache.v2" not in script
    assert 'id="kb-search-form"' in page
    assert 'id="kb-results"' in page
    assert 'id="kb-detail"' in page


def test_frontend_projects_internal_results_to_safe_fields() -> None:
    script = (PROJECT_ROOT / "frontend" / "js" / "evidence.js").read_text(encoding="utf-8")

    assert "function sanitizeResult" in script
    assert "item?.preview" in script
    assert "item?.detail" in script
    assert "item?.blocks" in script
    assert "item?.path" not in script
    assert "item?.source" not in script
    assert "source_type" not in script
    assert "rerank_score" not in script
    assert "retrieval_trace" not in script
    assert "core_node_ids" not in script
    assert "source_span_ids" not in script


def test_search_results_are_paginated_locally_without_more_api_calls() -> None:
    script = (PROJECT_ROOT / "frontend" / "js" / "evidence.js").read_text(encoding="utf-8")
    page = (PROJECT_ROOT / "frontend" / "pages" / "evidence.html").read_text(encoding="utf-8")
    styles = (PROJECT_ROOT / "frontend" / "css" / "evidence-page.css").read_text(encoding="utf-8")

    assert "const PAGE_SIZE = 4" in script
    assert "const SEARCH_RESULT_LIMIT = 24" in script
    assert "top_k: SEARCH_RESULT_LIMIT" in script
    assert "function renderPagination" in script
    assert "function setPage" in script
    set_page_body = script.split("function setPage", 1)[1].split("function boot", 1)[0]
    assert "apiPost" not in set_page_body
    assert 'id="kb-pagination"' in page
    assert 'id="kb-page-prev"' in page
    assert 'id="kb-page-numbers"' in page
    assert 'id="kb-page-next"' in page
    assert "repeat(4, minmax(106px, 1fr))" in styles
    assert "minmax(480px, 52%) minmax(0, 48%)" in styles
    assert "const pageStart = (state.page - 1) * PAGE_SIZE;" in script
    assert "String(pageStart + index + 1).padStart(2, '0')" in script


def test_pagination_appears_only_after_four_results_without_reserving_space() -> None:
    script = (PROJECT_ROOT / "frontend" / "js" / "evidence.js").read_text(encoding="utf-8")
    styles = (PROJECT_ROOT / "frontend" / "css" / "evidence-page.css").read_text(encoding="utf-8")

    page_size = int(script.split("const PAGE_SIZE = ", 1)[1].split(";", 1)[0])
    assert not (4 > page_size)
    assert 5 > page_size
    assert "const showPagination = totalResults > PAGE_SIZE;" in script
    assert "pagination.hidden = !showPagination;" in script
    assert "classList.toggle('has-pagination', showPagination)" in script
    assert script.count("setPaginationVisibility(0)") >= 2

    base_results_pane = styles.split(".kb-results-pane {", 1)[1].split("}", 1)[0]
    paginated_results_pane = styles.split(".kb-results-pane.has-pagination {", 1)[1].split("}", 1)[0]
    assert "grid-template-rows" not in base_results_pane
    assert "grid-template-rows: 49px minmax(0, 1fr) 45px" in paginated_results_pane


def test_detail_renders_only_sanitized_semantic_blocks() -> None:
    script = (PROJECT_ROOT / "frontend" / "js" / "evidence.js").read_text(encoding="utf-8")
    styles = (PROJECT_ROOT / "frontend" / "css" / "evidence-page.css").read_text(encoding="utf-8")

    assert "function sanitizeBlocks" in script
    assert "new Set(['heading', 'paragraph', 'list'])" in script
    assert "function renderDetailBlock" in script
    assert "function groupDetailSections" in script
    assert "function renderDetailSection" in script
    assert "const sections = groupDetailSections(item.blocks)" in script
    assert "const sectionMarkup = sections.map((section) => {" in script
    assert "const number = section.heading ? ++sectionNumber : 0;" in script
    assert "return renderDetailSection(section, number);" in script
    assert "block.items" in script
    assert "highlighted(block.text, true)" in script
    assert "highlighted(item, true)" in script
    assert '<p class="kb-detail-text">' not in script
    assert ".kb-detail-block-heading" in styles
    assert ".kb-detail-paragraph" in styles
    assert ".kb-detail-list" in styles
    assert ".kb-detail-mark" in styles
    assert ".kb-detail-section + .kb-detail-section" in styles
    assert "border-top: 1px solid var(--kb-line)" in styles
    assert ".kb-detail-section-body" in styles
    assert "kb-detail-section-index" in script
    assert "String(sectionNumber).padStart(2, '0')" in script
    assert ".kb-detail-section-heading" in styles
    assert ".kb-detail-section-index" in styles
    assert ".kb-detail-block-heading::before" not in styles


def test_result_rows_are_separate_units_without_a_selected_edge_bar() -> None:
    script = (PROJECT_ROOT / "frontend" / "js" / "evidence.js").read_text(encoding="utf-8")
    styles = (PROJECT_ROOT / "frontend" / "css" / "evidence-page.css").read_text(encoding="utf-8")

    result_grid = styles.split(".kb-results.is-paged {", 1)[1].split("}", 1)[0]
    result_row = styles.split(".kb-result-row {", 1)[1].split("}", 1)[0]
    selected_row = styles.split(".kb-result-row.active {", 1)[1].split("}", 1)[0]
    preview = styles.split(".kb-result-preview {", 1)[1].split("}", 1)[0]

    assert "gap: 10px" in result_grid
    assert "border: 1px solid var(--kb-line)" in result_row
    assert "border-radius: 6px" in result_row
    assert "min-height: 106px" in result_row
    assert "border-color: #b9afe9" in selected_row
    assert "background: #f4f1ff" in selected_row
    assert "inset" not in selected_row
    assert "box-shadow" not in selected_row
    assert ".kb-result-row.active::before" not in styles
    assert ".kb-result-row.active::after" not in styles
    assert "-webkit-line-clamp: 2" in preview
    assert "height: 3.1em" in preview
    assert "clip(item.title, 36)" in script
    assert "const contentType = cleanContentType" in script
    assert "function inferContentType" in script
    assert "|| inferContentType(title, chapter, blocks)" in script
    assert "clinical_feature: '临床表现'" in script
    assert "item?.score" not in script


def test_result_layout_adapts_to_low_height_desktop_and_mobile() -> None:
    styles = (PROJECT_ROOT / "frontend" / "css" / "evidence-page.css").read_text(encoding="utf-8")

    low_height = styles.split("@media (max-height: 720px) and (min-width: 901px) {", 1)[1]
    low_height = low_height.split("@media (max-width: 1050px)", 1)[0]
    assert "repeat(4, minmax(94px, 1fr))" in low_height
    assert "grid-template-rows: 45px minmax(0, 1fr) 41px" in low_height
    assert "min-height: 94px" in low_height

    compact = styles.split("@media (max-width: 900px) {", 1)[1]
    compact = compact.split("@media (max-width: 520px)", 1)[0]
    assert "height: calc(100dvh - var(--nav-height))" in compact
    assert "grid-template-rows: 52px minmax(0, 1fr)" in compact
    assert "overflow: hidden" in compact
    assert compact.count("height: 100%") >= 2

    mobile = styles.split("@media (max-width: 520px) {", 1)[1]
    assert "repeat(4, minmax(112px, 1fr))" in mobile
    assert "min-height: 112px" in mobile


def test_search_page_omits_old_overview_and_rejected_copy() -> None:
    files = (
        PROJECT_ROOT / "frontend" / "pages" / "evidence.html",
        PROJECT_ROOT / "frontend" / "js" / "evidence.js",
        PROJECT_ROOT / "frontend" / "css" / "evidence-page.css",
    )
    visible_bundle = "\n".join(path.read_text(encoding="utf-8") for path in files)

    for rejected in (
        "口腔医学知识总览",
        "学科知识分布",
        "维度分布",
        "概念索引",
        "下钻",
        "工作台",
        "多源支持",
        "支持",
        "来源",
        "已审核",
        "未审核",
        "证据",
    ):
        assert rejected not in visible_bundle
