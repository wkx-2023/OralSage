from __future__ import annotations

import json
from typing import Any, Callable
from unittest.mock import patch

import pytest

import backend.services.knowledge_search_retrieval as retrieval_module
from backend.services.knowledge_search_retrieval import (
    reset_knowledge_search_plan_cache,
    reset_knowledge_search_retrieval_cache,
    retrieve_knowledge_search_chunks,
)


MODULE = "backend.services.knowledge_search_retrieval"


def _item(doc_id: str, channel: str, layer: str = "textbook_evidence") -> dict[str, Any]:
    return {
        "doc_id": doc_id,
        "doc_type": "chunk",
        "source_layer": layer,
        "matched_by": [channel],
        "retrieval_score": 1.0,
    }


def _row(
    text: str,
    *,
    layer: str = "textbook_evidence",
    subject: str = "牙体牙髓病学",
    chapter: str = "牙髓病",
    heading: str = "临床表现",
) -> dict[str, Any]:
    return {
        "text": text,
        "source_layer": layer,
        "source_type": "textbook",
        "subject": subject,
        "chapter": chapter,
        "heading_path": [chapter, heading],
        "source_title": f"{subject}.pdf",
        "metadata": {},
    }


def _pack(
    *,
    sparse: list[dict[str, Any]] | None = None,
    dense: list[dict[str, Any]] | None = None,
    ppr: list[dict[str, Any]] | None = None,
    candidates: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    sparse = sparse or []
    dense = dense or []
    ppr = ppr or []
    if candidates is None:
        candidates = [*sparse, *dense, *ppr]
    return {
        "aliases": [],
        "sparse": sparse,
        "dense": dense,
        "ppr": ppr,
        "candidates": candidates,
        "gated_candidates": candidates,
    }


class _FakeSolver:
    def __init__(self, rows: dict[str, dict[str, Any]], pack_for: Callable[[str], dict[str, Any]]) -> None:
        self.chunks = rows
        self._pack_for = pack_for
        self.queries: list[str] = []

    def retrieve_candidates(self, query: str, **_: Any) -> dict[str, Any]:
        self.queries.append(query)
        return self._pack_for(query)


@pytest.fixture(autouse=True)
def _clear_plan_cache() -> None:
    reset_knowledge_search_plan_cache()
    reset_knowledge_search_retrieval_cache()
    yield
    reset_knowledge_search_plan_cache()
    reset_knowledge_search_retrieval_cache()


def test_dense_only_candidate_survives_sparse_first_union_and_reaches_reranker() -> None:
    sparse = [_item(f"chunk:sparse-{index}", "bm25") for index in range(30)]
    dense_hit = _item("chunk:dense-hit", "dense_bge_m3")
    candidates = [*sparse, dense_hit]
    rows = {
        item["doc_id"]: _row(f"牙周基础知识片段 {index}。")
        for index, item in enumerate(sparse)
    }
    rows[dense_hit["doc_id"]] = _row(
        "冷刺激引起一过性疼痛，刺激去除后立即消失，可见于牙本质敏感或可复性牙髓炎。",
        heading="可复性牙髓炎",
    )
    solver = _FakeSolver(rows, lambda _: _pack(sparse=sparse, dense=[dense_hit], candidates=candidates))
    captured: list[Any] = []

    def rerank(_query: str, chunks: list[Any], top_k: int):
        captured.extend(chunks)
        return chunks[:top_k], "lexical_fallback"

    with (
        patch(f"{MODULE}.is_llm_configured", return_value=False),
        patch(f"{MODULE}.get_kag_solver", return_value=solver),
        patch(f"{MODULE}.rerank_chunks", side_effect=rerank),
    ):
        results = retrieve_knowledge_search_chunks("牙齿喝凉水酸痛一下马上就好是什么情况")

    assert candidates.index(dense_hit) == 30
    assert "chunk:dense-hit" in [chunk.id for chunk in captured]
    assert results[0].id == "chunk:dense-hit"
    assert "牙体牙髓病学" in captured[0].title
    assert "可复性牙髓炎" in captured[0].title


def test_obviously_nonoral_external_reference_is_filtered_before_reranking() -> None:
    nonoral = _item("chunk:hemolysis", "dense_bge_m3", "external_reference")
    oral = _item("chunk:tooth-pain", "dense_bge_m3", "external_reference")
    rows = {
        nonoral["doc_id"]: _row(
            "喝冷水后低温抗体与红细胞结合，可能导致血管内溶血和血红蛋白尿。",
            layer="external_reference",
            subject="",
            chapter="冷凝集素病",
            heading="临床表现",
        ),
        oral["doc_id"]: _row(
            "牙髓对冷刺激产生短暂疼痛，牙本质暴露也可造成牙齿敏感。",
            layer="external_reference",
            subject="",
            chapter="牙痛和感染",
            heading="牙痛的特征",
        ),
    }
    solver = _FakeSolver(rows, lambda _: _pack(dense=[nonoral, oral]))
    captured: list[Any] = []

    def rerank(_query: str, chunks: list[Any], top_k: int):
        captured.extend(chunks)
        return chunks[:top_k], "lexical_fallback"

    with (
        patch(f"{MODULE}.is_llm_configured", return_value=False),
        patch(f"{MODULE}.get_kag_solver", return_value=solver),
        patch(f"{MODULE}.rerank_chunks", side_effect=rerank),
    ):
        results = retrieve_knowledge_search_chunks("喝凉水牙齿疼")

    assert [chunk.id for chunk in captured] == ["chunk:tooth-pain"]
    assert [chunk.id for chunk in results] == ["chunk:tooth-pain"]


def test_learning_objective_chunk_is_filtered_before_reranking() -> None:
    objective = _item("chunk:objective", "dense_bge_m3")
    clinical = _item("chunk:clinical", "dense_bge_m3")
    rows = {
        objective["doc_id"]: _row("掌握：口腔扁平苔藓的定义、临床表现、诊断与鉴别诊断。", subject="口腔黏膜病学"),
        clinical["doc_id"]: _row(
            "口腔扁平苔藓常呈珠光白色网状条纹，部分患者伴烧灼感。",
            subject="口腔黏膜病学",
        ),
    }
    solver = _FakeSolver(rows, lambda _: _pack(dense=[objective, clinical]))
    captured: list[Any] = []

    def rerank(_query: str, chunks: list[Any], top_k: int):
        captured.extend(chunks)
        return chunks[:top_k], "lexical_fallback"

    with (
        patch(f"{MODULE}.is_llm_configured", return_value=False),
        patch(f"{MODULE}.get_kag_solver", return_value=solver),
        patch(f"{MODULE}.rerank_chunks", side_effect=rerank),
    ):
        retrieve_knowledge_search_chunks("口腔扁平苔藓临床表现")

    assert [chunk.id for chunk in captured] == ["chunk:clinical"]


def test_llm_planner_failure_is_silent_and_cached_rule_queries_are_used() -> None:
    hit = _item("chunk:hit", "dense_bge_m3")
    solver = _FakeSolver(
        {hit["doc_id"]: _row("牙齿受到冷刺激后出现一过性疼痛。")},
        lambda _: _pack(dense=[hit]),
    )

    with (
        patch.dict("os.environ", {"KNOWLEDGE_SEARCH_LLM_PLANNER_MODE": "always"}),
        patch(f"{MODULE}.is_llm_configured", return_value=True),
        patch(f"{MODULE}._planner_completion", side_effect=RuntimeError("planner unavailable")) as llm,
        patch(f"{MODULE}.get_kag_solver", return_value=solver),
        patch(f"{MODULE}.rerank_chunks", side_effect=lambda _q, chunks, k: (chunks[:k], "lexical_fallback")),
    ):
        first = retrieve_knowledge_search_chunks("牙齿喝凉水酸痛一下马上就好是什么情况")
        second = retrieve_knowledge_search_chunks("牙齿喝凉水酸痛一下马上就好是什么情况")

    assert first and second
    assert solver.queries[0] == "牙齿喝凉水酸痛一下马上就好是什么情况"
    assert any("冷刺激" in query and "刺激去除后立即消失" in query for query in solver.queries)
    assert llm.call_count == 1


def test_llm_planner_keeps_original_and_caps_total_query_routes_at_three() -> None:
    hit = _item("chunk:hit", "dense_bge_m3")
    solver = _FakeSolver({hit["doc_id"]: _row("牙周炎导致牙槽骨吸收。")}, lambda _: _pack(dense=[hit]))
    planner_payload = json.dumps(
        {
            "normalized_query": "牙周炎进展导致牙齿松动的机制",
            "expanded_queries": ["牙槽骨吸收与牙周支持丧失", "咬合创伤与牙松动", "不应执行的第四路"],
        },
        ensure_ascii=False,
    )

    with (
        patch.dict("os.environ", {"KNOWLEDGE_SEARCH_LLM_PLANNER_MODE": "always"}),
        patch(f"{MODULE}.is_llm_configured", return_value=True),
        patch(f"{MODULE}._planner_completion", return_value=planner_payload),
        patch(f"{MODULE}.get_kag_solver", return_value=solver),
        patch(f"{MODULE}.rerank_chunks", side_effect=lambda _q, chunks, k: (chunks[:k], "lexical_fallback")),
    ):
        retrieve_knowledge_search_chunks("为什么牙周炎发展后牙齿会逐渐松动")

    assert solver.queries == [
        "为什么牙周炎发展后牙齿会逐渐松动",
        "牙周炎进展导致牙齿松动的机制",
        "牙槽骨吸收与牙周支持丧失",
    ]


def test_colloquial_rule_normalization_avoids_an_unnecessary_llm_request() -> None:
    hit = _item("chunk:hit", "dense_bge_m3")
    solver = _FakeSolver({hit["doc_id"]: _row("冷刺激后出现一过性牙痛。")}, lambda _: _pack(dense=[hit]))

    with (
        patch(f"{MODULE}.is_llm_configured", return_value=True),
        patch(f"{MODULE}._planner_completion") as planner,
        patch(f"{MODULE}.get_kag_solver", return_value=solver),
        patch(f"{MODULE}.rerank_chunks", side_effect=lambda _q, chunks, k: (chunks[:k], "lexical_fallback")),
    ):
        retrieve_knowledge_search_chunks("牙齿喝凉水酸痛一下马上就好是什么情况")

    planner.assert_not_called()
    assert solver.queries == [
        "牙齿喝凉水酸痛一下马上就好是什么情况",
        "牙齿冷刺激一过性疼痛，刺激去除后立即消失",
    ]


def test_oral_white_reticular_lesion_adds_a_bounded_differential_query() -> None:
    hit = _item("chunk:lichen", "dense_bge_m3")
    solver = _FakeSolver(
        {hit["doc_id"]: _row("口腔扁平苔藓可见白色网状条纹。", subject="口腔黏膜病学")},
        lambda _: _pack(dense=[hit]),
    )

    with (
        patch(f"{MODULE}.is_llm_configured", return_value=True),
        patch(f"{MODULE}._planner_completion") as planner,
        patch(f"{MODULE}.get_kag_solver", return_value=solver),
        patch(f"{MODULE}.rerank_chunks", side_effect=lambda _q, chunks, k: (chunks[:k], "lexical_fallback")),
    ):
        retrieve_knowledge_search_chunks("嘴里反复出现擦不掉的白色网状条纹，还有烧灼感，可能是什么情况")

    planner.assert_not_called()
    assert len(solver.queries) == 2
    assert solver.queries[0].startswith("嘴里反复出现")
    assert solver.queries[1] == "口腔扁平苔藓 典型临床表现 白色网状条纹 Wickham纹 烧灼感"


def test_page_specific_planner_request_uses_a_strict_network_timeout() -> None:
    observed: dict[str, float] = {}

    def timeout_request(_request: Any, *, timeout: float):
        observed["timeout"] = timeout
        raise TimeoutError("network stalled")

    with (
        patch.dict("os.environ", {"KNOWLEDGE_SEARCH_LLM_API_KEY": "test-key"}),
        patch(f"{MODULE}.urlopen", side_effect=timeout_request),
        pytest.raises(RuntimeError, match="planner request failed"),
    ):
        retrieval_module._planner_completion([{"role": "user", "content": "test"}])

    assert 1.0 <= observed["timeout"] <= 8.0


def test_qwen_scores_use_relative_cutoff_but_keep_a_reasonable_minimum() -> None:
    dense = [_item(f"chunk:{index}", "dense_bge_m3") for index in range(10)]
    rows = {item["doc_id"]: _row(f"牙周炎相关知识 {index}。") for index, item in enumerate(dense)}
    solver = _FakeSolver(rows, lambda _: _pack(dense=dense))
    scores = [0.95, 0.70, 0.48, 0.30, 0.22, 0.18, 0.12, 0.08, 0.05, 0.01]

    def rerank(_query: str, chunks: list[Any], top_k: int):
        ranked = chunks[:top_k]
        for chunk, score in zip(ranked, scores):
            chunk.metadata["rerank_score"] = score
        return ranked, "qwen3-rerank"

    with (
        patch(f"{MODULE}.is_llm_configured", return_value=False),
        patch(f"{MODULE}.get_kag_solver", return_value=solver),
        patch(f"{MODULE}.rerank_chunks", side_effect=rerank),
    ):
        results = retrieve_knowledge_search_chunks("牙周炎", top_k=10)

    assert len(results) == 4
    assert results[-1].metadata["rerank_score"] == 0.30


def test_symptom_intent_calibration_prefers_direct_clinical_content() -> None:
    etiology = _item("chunk:etiology", "dense_bge_m3")
    clinical = _item("chunk:clinical", "dense_bge_m3")
    rows = {
        etiology["doc_id"]: _row(
            "感染因素可能参与口腔扁平苔藓的发生，但相关机制仍未完全明确。",
            subject="口腔黏膜病学",
            chapter="口腔斑纹类疾病",
            heading="病因和发病机制",
        ),
        clinical["doc_id"]: _row(
            "口腔扁平苔藓常表现为白色网状条纹，部分患者伴有烧灼感。",
            subject="口腔黏膜病学",
            chapter="口腔斑纹类疾病",
            heading="临床表现",
        ),
    }
    solver = _FakeSolver(rows, lambda _: _pack(dense=[etiology, clinical]))

    def rerank(_query: str, chunks: list[Any], top_k: int):
        by_id = {chunk.id: chunk for chunk in chunks}
        by_id["chunk:etiology"].metadata["rerank_score"] = 0.88
        by_id["chunk:clinical"].metadata["rerank_score"] = 0.82
        return [by_id["chunk:etiology"], by_id["chunk:clinical"]][:top_k], "qwen3-rerank"

    with (
        patch(f"{MODULE}.is_llm_configured", return_value=False),
        patch(f"{MODULE}.get_kag_solver", return_value=solver),
        patch(f"{MODULE}.rerank_chunks", side_effect=rerank),
    ):
        results = retrieve_knowledge_search_chunks(
            "嘴里反复出现擦不掉的白色网状条纹，还有烧灼感，可能是什么情况"
        )

    assert results[0].id == "chunk:clinical"


def test_duplicate_text_is_sent_to_reranker_only_once() -> None:
    first = _item("chunk:first", "dense_bge_m3")
    duplicate = _item("chunk:duplicate", "bm25")
    repeated = "牙周基础治疗包括口腔卫生指导、洁治、刮治和根面平整。"
    rows = {
        first["doc_id"]: _row(repeated, subject="牙周病学"),
        duplicate["doc_id"]: _row(repeated, subject="牙周病学"),
    }
    solver = _FakeSolver(rows, lambda _: _pack(sparse=[duplicate], dense=[first]))
    captured: list[Any] = []

    def rerank(_query: str, chunks: list[Any], top_k: int):
        captured.extend(chunks)
        return chunks[:top_k], "lexical_fallback"

    with (
        patch(f"{MODULE}.is_llm_configured", return_value=False),
        patch(f"{MODULE}.get_kag_solver", return_value=solver),
        patch(f"{MODULE}.rerank_chunks", side_effect=rerank),
    ):
        retrieve_knowledge_search_chunks("牙周基础治疗")

    assert len(captured) == 1


def test_final_results_are_process_cached_and_returned_as_independent_copies() -> None:
    hit = _item("chunk:cached", "dense_bge_m3")
    solver = _FakeSolver({hit["doc_id"]: _row("牙周炎会造成牙周支持组织破坏。")}, lambda _: _pack(dense=[hit]))

    with (
        patch(f"{MODULE}.is_llm_configured", return_value=False),
        patch(f"{MODULE}.get_kag_solver", return_value=solver),
        patch(
            f"{MODULE}.rerank_chunks",
            side_effect=lambda _q, chunks, k: (chunks[:k], "lexical_fallback"),
        ) as rerank,
    ):
        first = retrieve_knowledge_search_chunks("牙周炎")
        first[0].metadata["caller_mutation"] = True
        second = retrieve_knowledge_search_chunks("牙周炎")

    assert len(solver.queries) == 1
    assert rerank.call_count == 1
    assert "caller_mutation" not in second[0].metadata
