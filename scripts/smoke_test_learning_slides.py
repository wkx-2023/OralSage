"""Smoke test for slides builders.

Covers:
1. ``build_slides_from_handout_and_tutor`` — pure function (dict → dict).
2. ``build_and_save_slides_from_session_cache`` — two-step: single slides + deck.
3. ``generate_slides_resource_from_session_cache`` — Resource Factory entry point.
4. Optional handout modules — empty clinical_example / common_confusions produce no blocks.
5. ``build_slide_from_handout_section`` — single slide pure builder.
6. ``build_and_save_slide_for_node_from_session_cache`` — single node slide cache.
7. ``aggregate_slides_from_node_cache`` — deck from single-slide caches (partial ok).

Uses fixed fixtures + ``tempfile.TemporaryDirectory`` — no LLM, no server,
no database, no writes to real ``data/learning``.

Run from the project root:

    python scripts/smoke_test_learning_slides.py

Expected last line: ``learning slides smoke test passed``
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

# Ensure the project root is on sys.path
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from backend.services.learning.handout_cache import save_handout_cache_ready
from backend.services.learning.resource_factory import (
    generate_slides_resource_from_session_cache,
)
from backend.services.learning.slides import (
    aggregate_slides_from_node_cache,
    build_and_save_slide_for_node_from_session_cache,
    build_and_save_slides_from_session_cache,
    build_slide_from_handout_section,
    build_slides_from_handout_and_tutor,
)
from backend.services.learning.tutor_cache import save_tutor_cache_ready


# ============================================================================
# Assertion helpers
# ============================================================================

_FAILED = 0


def _check(condition: bool, label: str) -> None:
    global _FAILED
    if not condition:
        print(f"  FAIL: {label}")
        _FAILED += 1
    else:
        print(f"  ok: {label}")


# ============================================================================
# Shared fixtures
# ============================================================================


def _make_handout_section_node_1() -> dict:
    return {
        "outline_node_id": "node_1",
        "title": "牙周炎的定义与病因",
        "section_status": "llm_generated",
        "evidence_status": "sufficient",
        "used_evidence": {
            "kg_node_ids": ["kg://perio/001", "kg://perio/002"],
            "kg_edge_ids": ["edge_001"],
            "concept_ids": [],
            "question_ids": [],
            "evidence_chunk_ids": ["chunk_a", "chunk_b"],
        },
        "content": {
            "overview": "牙周炎是牙齿支持组织的慢性炎症性疾病，由菌斑微生物引发。",
            "key_points": [
                {
                    "heading": "牙周炎的定义",
                    "explanation": "牙周炎是一种累及牙龈、牙周膜、牙槽骨的慢性感染性疾病。",
                    "evidence_refs": ["chunk_a"],
                },
                {
                    "heading": "主要病因：菌斑生物膜",
                    "explanation": "菌斑生物膜是牙周炎最主要的始动因子。",
                    "evidence_refs": ["chunk_b"],
                },
                {
                    "heading": "危险因素",
                    "explanation": "吸烟、糖尿病、遗传因素等是重要的危险因素。",
                    "evidence_refs": [],
                },
            ],
            "clinical_example": "患者男，45岁，吸烟20年，主诉牙龈出血、牙齿松动。检查发现深牙周袋、附着丧失。",
            "common_confusions": [
                {
                    "confusion": "牙龈炎 vs 牙周炎",
                    "clarification": "牙龈炎仅累及牙龈，无附着丧失；牙周炎有附着丧失和牙槽骨吸收。",
                    "evidence_refs": [],
                },
            ],
            "transition_to_next": "了解了定义和病因后，接下来学习牙周炎的分类与临床表现。",
        },
        "missing_evidence": [],
    }


def _make_handout_section_node_2() -> dict:
    return {
        "outline_node_id": "node_2",
        "title": "牙周炎的分类与临床表现",
        "section_status": "llm_generated",
        "evidence_status": "sufficient",
        "used_evidence": {
            "kg_node_ids": ["kg://perio/003"],
            "kg_edge_ids": [],
            "concept_ids": [],
            "question_ids": [],
            "evidence_chunk_ids": ["chunk_c"],
        },
        "content": {
            "overview": "牙周炎根据疾病进展和范围可分为慢性牙周炎和侵袭性牙周炎两大类。",
            "key_points": [
                {
                    "heading": "慢性牙周炎",
                    "explanation": "最常见类型，进展缓慢，多见于成年人。",
                    "evidence_refs": ["chunk_c"],
                },
                {
                    "heading": "侵袭性牙周炎",
                    "explanation": "进展迅速，多见于年轻患者。",
                    "evidence_refs": [],
                },
            ],
            "clinical_example": "患者女，22岁，口腔卫生良好但多个牙位出现深牙周袋和骨吸收。",
            "common_confusions": [
                {
                    "confusion": "慢性 vs 侵袭性牙周炎",
                    "clarification": "慢性进展慢、与菌斑量一致；侵袭性进展快、与菌斑量不成比例。",
                    "evidence_refs": [],
                },
            ],
            "transition_to_next": "掌握分类后，可进一步学习牙周炎的治疗原则。",
        },
        "missing_evidence": [],
    }


def _make_tutor_step_node_1() -> dict:
    return {
        "outline_node_id": "node_1",
        "teaching_text": "今天我们学习牙周炎的定义与病因。牙周炎是累及牙周支持组织的慢性炎症……",
        "questions": [
            {
                "id": "q_node_1_1",
                "type": "single_choice",
                "stem": "牙周炎最主要的始动因子是？",
                "options": ["A. 遗传", "B. 菌斑生物膜", "C. 吸烟", "D. 糖尿病"],
                "answer": "B",
                "explanation": "菌斑生物膜是牙周炎最主要的始动因子。",
            },
        ],
        "quiz_scope": "section",
        "quiz_timing": "after_section",
        "content_status": "llm_generated",
    }


def _make_tutor_step_node_2() -> dict:
    return {
        "outline_node_id": "node_2",
        "teaching_text": "接下来学习牙周炎的分类。主要分为慢性牙周炎和侵袭性牙周炎两大类……",
        "questions": [],  # empty — should produce warning
        "quiz_scope": "section",
        "quiz_timing": "after_section",
        "content_status": "llm_generated",
    }


def _make_outline_3nodes() -> dict:
    """Standard 3-node outline used by multiple tests."""
    return {
        "title": "牙周炎基础学习",
        "nodes": [
            {
                "id": "node_1",
                "title": "牙周炎的定义与病因",
                "learning_objectives": ["理解牙周炎的定义", "掌握主要病因"],
                "sequence_reason": "从定义和病因开始",
                "kg_node_ids": ["kg://perio/001", "kg://perio/002"],
            },
            {
                "id": "node_2",
                "title": "牙周炎的分类与临床表现",
                "learning_objectives": ["掌握牙周炎分类", "识别典型临床表现"],
                "sequence_reason": "病因之后自然进入分类",
                "kg_node_ids": ["kg://perio/003"],
            },
            {
                "id": "node_3",
                "title": "牙周炎的治疗原则",
                "learning_objectives": ["掌握治疗原则"],
                "sequence_reason": "分类之后进入治疗",
                "kg_node_ids": ["kg://perio/004"],
            },
        ],
    }


# ============================================================================
# Test 1: pure function (build_slides_from_handout_and_tutor)
# ============================================================================


def test_pure_function() -> None:
    """Existing pure-function test — dict insertion order + mismatch + warnings."""
    print("=" * 60)
    print("=== TEST 1: build_slides_from_handout_and_tutor (pure function) ===")
    print("=" * 60)

    session_id = "test_session"

    # -- outline (3 nodes: node_1, node_2, node_3) ----------------------------
    outline = _make_outline_3nodes()

    # -- handout_sections (node_2 first to test ordering) ---------------------
    handout_sections = {
        "node_2": _make_handout_section_node_2(),
        "node_1": _make_handout_section_node_1(),
        # mismatch: key="node_3" but outline_node_id="node_99" -> skipped
        "node_3": {
            "outline_node_id": "node_99",
            "title": "mismatch",
            "section_status": "llm_generated",
            "evidence_status": "sufficient",
            "used_evidence": {"kg_node_ids": [], "kg_edge_ids": [], "concept_ids": [], "question_ids": [], "evidence_chunk_ids": []},
            "content": {
                "overview": "...",
                "key_points": [{"heading": "x", "explanation": "x", "evidence_refs": []}],
                "clinical_example": "",
                "common_confusions": [],
                "transition_to_next": "",
            },
            "missing_evidence": [],
        },
    }

    # -- tutor_steps ----------------------------------------------------------
    tutor_steps = {
        "node_1": _make_tutor_step_node_1(),
        "node_2": _make_tutor_step_node_2(),
    }

    result = build_slides_from_handout_and_tutor(
        session_id=session_id,
        outline=outline,
        handout_sections_by_node_id=handout_sections,
        tutor_steps_by_node_id=tutor_steps,
        deck_title=None,
        theme_id="clean_light",
    )

    # -- Top-level schema -----------------------------------------------------
    _check(result["schema_version"] == "slides.v1", "schema_version == 'slides.v1'")
    _check(result["deck_id"] == "deck_test_session", "deck_id == 'deck_test_session'")
    _check(result["session_id"] == "test_session", "session_id == 'test_session'")
    _check(result["theme_id"] == "clean_light", "theme_id == 'clean_light'")
    _check(result["source"]["type"] == "handout_sections+tutor_steps", "source.type")
    _check(result["source"]["granularity"] == "one_slide_per_outline_node", "source.granularity")
    _check(result["deck_title"] == "牙周炎基础学习", "deck_title from outline.title")

    slides = result["slides"]
    warnings = result.get("warnings", [])

    _check(len(slides) == 2, "slides count == 2 (node_3 mismatch skipped)")

    # -- Ordering -------------------------------------------------------------
    _check(slides[0]["outline_node_id"] == "node_1", "slide[0] is node_1 (outline order, not dict insertion)")
    _check(slides[1]["outline_node_id"] == "node_2", "slide[1] is node_2")

    # -- Mismatch warning -----------------------------------------------------
    mismatch_warnings = [w for w in warnings if "node_99" in w and "node_3" in w]
    _check(len(mismatch_warnings) >= 1, f"mismatch warning for node_3/node_99: found {len(mismatch_warnings)}")

    # -- Input fixture still has transition_to_next (prove no mutation) --------
    _check("transition_to_next" in handout_sections["node_1"]["content"],
           "input node_1 fixture still has transition_to_next")
    _check("transition_to_next" in handout_sections["node_2"]["content"],
           "input node_2 fixture still has transition_to_next")

    # -- Per-slide assertions -------------------------------------------------
    for i, slide in enumerate(slides):
        node_id = slide["outline_node_id"]

        _check(slide["outline_node_id"] == slide["section_id"], f"{node_id}: outline_node_id == section_id")
        _check(slide["slide_id"] == f"slide_{node_id}", f"{node_id}: slide_id")
        _check(slide["slide_type"] == "mixed_learning", f"{node_id}: slide_type")
        _check(bool(slide["title"]) and slide["title"] != "未命名小节", f"{node_id}: title='{slide['title']}'")
        _check(slide["subtitle"] == "", f"{node_id}: subtitle empty string")

        bullets = slide["bullets"]
        _check(isinstance(bullets, list) and len(bullets) > 0, f"{node_id}: bullets non-empty ({len(bullets)})")
        _check(len(bullets) <= 5, f"{node_id}: bullets <= 5")
        for b in bullets:
            _check(len(b) < 80, f"{node_id}: bullet short (not explanation): '{b[:40]}'")

        _check(slide["narration_ref"]["source"] == "tutor_step.teaching_text", f"{node_id}: narration_ref.source")
        _check(slide["narration_ref"]["outline_node_id"] == node_id, f"{node_id}: narration_ref.outline_node_id")
        _check(slide["quiz_ref"]["source"] == "tutor_step.questions", f"{node_id}: quiz_ref.source")
        _check(slide["quiz_ref"]["outline_node_id"] == node_id, f"{node_id}: quiz_ref.outline_node_id")
        _check(slide["tutor_step_ref"] == node_id, f"{node_id}: tutor_step_ref")
        _check(slide["visual_hint"]["diagram_type"] == "mixed_learning_card", f"{node_id}: visual_hint")

        expected_kg = handout_sections[node_id]["used_evidence"]["kg_node_ids"]
        _check(slide["related_kg_node_ids"] == expected_kg, f"{node_id}: related_kg_node_ids")

        blocks = slide["blocks"]
        _check(isinstance(blocks, list) and len(blocks) > 0, f"{node_id}: blocks non-empty ({len(blocks)})")
        block_types = {b["block_type"] for b in blocks}
        for bt in ("overview", "key_point", "clinical_example", "confusion"):
            _check(bt in block_types, f"{node_id}: blocks contains {bt}")
        _check("transition" not in block_types, f"{node_id}: blocks does NOT contain transition")

        # Output slide content must NOT contain transition_to_next
        _check("transition_to_next" not in slide["content"],
               f"{node_id}: content does NOT contain transition_to_next")

        # But required fields are still present
        for _f in ("overview", "key_points", "clinical_example", "common_confusions"):
            _check(_f in slide["content"], f"{node_id}: content.{_f} present")

        for b in blocks:
            _check(bool(b.get("block_id")), f"{node_id}: block_id")
            _check(bool(b.get("block_type")), f"{node_id}: block_type")
            _check("title" in b, f"{node_id}: block.title")
            _check("body" in b, f"{node_id}: block.body")
            _check("source_field" in b, f"{node_id}: block.source_field")

        for kpb in [b for b in blocks if b["block_type"] == "key_point"]:
            _check(kpb.get("semantic_role") == "concept", f"{node_id}: key_point semantic_role='concept'")
            _check(isinstance(kpb.get("evidence_refs"), list), f"{node_id}: key_point evidence_refs list")

        _check(isinstance(slide["content"], dict), f"{node_id}: content is dict")
        _check("overview" in slide["content"], f"{node_id}: content.overview")
        _check("key_points" in slide["content"], f"{node_id}: content.key_points")

    # -- JSON string assertions: no transition_to_next anywhere ----------------
    result_json_str = json.dumps(result, ensure_ascii=False)
    _check("transition_to_next" not in result_json_str,
           "result JSON string does NOT contain 'transition_to_next'")
    # Specific transition text from fixtures must be absent
    _check("了解了定义和病因后" not in result_json_str,
           "result JSON string does NOT contain node_1 transition text")
    _check("掌握分类后" not in result_json_str,
           "result JSON string does NOT contain node_2 transition text")

    # -- Warning: node_2 empty questions --------------------------------------
    qw = [w for w in warnings if "questions is empty" in w]
    _check(len(qw) == 1, "1 warning for empty questions (node_2)")


# ============================================================================
# Test 2: session cache (build_and_save_slides_from_session_cache)
# ============================================================================


def test_session_cache() -> tuple[str, dict]:
    """Session-cache test using temp dir.

    Also verifies that the updated two-step pipeline creates single-slide
    cache files as intermediate artifacts.

    Returns (saved_slides_json_path_str, resource_json_dict) for later inspection.
    """
    print()
    print("=" * 60)
    print("=== TEST 2: build_and_save_slides_from_session_cache ===")
    print("=" * 60)

    session_id = "cache_test_session"

    outline = _make_outline_3nodes()

    with tempfile.TemporaryDirectory() as tmpdir:
        resource_root = Path(tmpdir)

        # -- Write handout caches (node_1, node_2 ready; node_3 intentionally missing) --
        save_handout_cache_ready(
            session_id, "node_1",
            _make_handout_section_node_1(),
            source="cache_smoke_test",
            resource_root=resource_root,
        )
        save_handout_cache_ready(
            session_id, "node_2",
            _make_handout_section_node_2(),
            source="cache_smoke_test",
            resource_root=resource_root,
        )
        # node_3: no handout cache — should produce warning and skip

        # -- Write tutor caches (node_1, node_2) --
        save_tutor_cache_ready(
            session_id, "node_1",
            _make_tutor_step_node_1(),
            source="cache_smoke_test",
            resource_root=resource_root,
        )
        save_tutor_cache_ready(
            session_id, "node_2",
            _make_tutor_step_node_2(),  # empty questions
            source="cache_smoke_test",
            resource_root=resource_root,
        )

        # -- Call the function --
        result = build_and_save_slides_from_session_cache(
            session_id=session_id,
            outline=outline,
            resource_root=resource_root,
            deck_title=None,
            theme_id="clean_light",
        )

        # -- Assert return structure --
        _check(result["status"] == "ok", f"status == 'ok' (got {result['status']!r})")
        _check(result["resource_type"] == "slides_json", f"resource_type == 'slides_json'")

        rpath = result["resource_path"]
        # Path should end with slides/slides.v1.json
        rpath_norm = rpath.replace("\\", "/")
        _check(rpath_norm.endswith("slides/slides.v1.json"),
               f"resource_path ends with slides/slides.v1.json: {rpath_norm}")

        # -- Assert file exists on disk --
        _check(os.path.isfile(rpath), f"file exists on disk: {rpath}")

        # -- Read saved JSON --
        with open(rpath, "r", encoding="utf-8") as f:
            saved_json = json.load(f)

        _check(saved_json["schema_version"] == "slides.v1", "saved: schema_version == 'slides.v1'")
        _check(saved_json["deck_id"] == f"deck_{session_id}", f"saved: deck_id == 'deck_{session_id}'")
        _check(saved_json["theme_id"] == "clean_light", "saved: theme_id == 'clean_light'")
        _check(saved_json["deck_title"] == "牙周炎基础学习", "saved: deck_title from outline")

        saved_slides = saved_json["slides"]
        saved_warnings = saved_json.get("warnings", [])

        # Two ready handout caches → 2 slides (node_3 missing handout → skipped)
        _check(len(saved_slides) == 2, f"saved: slides count == 2 (got {len(saved_slides)})")

        # Order must follow outline order
        _check(saved_slides[0]["outline_node_id"] == "node_1", "saved: slide[0] is node_1")
        _check(saved_slides[1]["outline_node_id"] == "node_2", "saved: slide[1] is node_2")

        # Verify per-slide identifiers + no transition blocks + no transition_to_next
        for s in saved_slides:
            nid = s["outline_node_id"]
            _check(s["outline_node_id"] == s["section_id"], f"saved: {nid} outline_node_id == section_id")
            _check(s["slide_id"] == f"slide_{nid}", f"saved: {nid} slide_id")

            # Blocks must NOT contain transition
            saved_block_types = {b["block_type"] for b in s["blocks"]}
            _check("transition" not in saved_block_types,
                   f"saved: {nid} blocks does NOT contain transition")

            # Output slide content must NOT contain transition_to_next
            _check("transition_to_next" not in s["content"],
                   f"saved: {nid} content does NOT contain transition_to_next")

            # Required fields still present
            for _f in ("overview", "key_points", "clinical_example", "common_confusions"):
                _check(_f in s["content"], f"saved: {nid} content.{_f} present")

        # -- JSON string assertions: no transition_to_next anywhere in saved file --
        saved_json_str = json.dumps(saved_json, ensure_ascii=False)
        _check("transition_to_next" not in saved_json_str,
               "saved JSON string does NOT contain 'transition_to_next'")
        _check("了解了定义和病因后" not in saved_json_str,
               "saved JSON string does NOT contain node_1 transition text")
        _check("掌握分类后" not in saved_json_str,
               "saved JSON string does NOT contain node_2 transition text")

        # Warnings: node_3 missing handout (may appear as handout cache status + missing_slide_cache)
        missing_h_warnings = [w for w in saved_warnings if "handout cache status" in w and "node_3" in w]
        _check(len(missing_h_warnings) == 1,
               f"exactly 1 warning for node_3 missing handout cache: found {len(missing_h_warnings)}")

        # Aggregation also reports missing_slide_cache for node_3
        missing_sc_warnings = [w for w in saved_warnings if "missing_slide_cache" in w and "node_3" in w]
        _check(len(missing_sc_warnings) >= 1,
               f"warning for missing_slide_cache node_3: found {len(missing_sc_warnings)}")

        empty_q_warnings = [w for w in saved_warnings if "questions is empty" in w]
        _check(len(empty_q_warnings) >= 1,
               f"warning for node_2 empty questions: found {len(empty_q_warnings)}")

        # -- NEW: Verify single slide cache files exist ---------------------------------
        slide_path_1 = resource_root / session_id / "slides" / "node_1.json"
        _check(slide_path_1.exists(),
               f"single slide cache exists: {slide_path_1}")
        slide_path_2 = resource_root / session_id / "slides" / "node_2.json"
        _check(slide_path_2.exists(),
               f"single slide cache exists: {slide_path_2}")
        slide_path_3 = resource_root / session_id / "slides" / "node_3.json"
        _check(not slide_path_3.exists(),
               f"single slide cache for node_3 does NOT exist (no handout)")

        # Verify single slide cache contents
        with open(slide_path_1, "r", encoding="utf-8") as f:
            node1_slide = json.load(f)
        _check(node1_slide["outline_node_id"] == "node_1", "node_1 single slide: outline_node_id")
        _check(node1_slide["slide_id"] == "slide_node_1", "node_1 single slide: slide_id")
        _check("transition_to_next" not in node1_slide.get("content", {}),
               "node_1 single slide: no transition_to_next in content")

        # -- No legacy slides.json in session root --
        legacy_path = resource_root / session_id / "slides.json"
        _check(not legacy_path.exists(),
               f"no legacy slides.json in session root: {legacy_path}")

        # -- Return the saved JSON for reporting --
        print()
        print("=== Saved slides.v1.json ===")
        print(json.dumps(saved_json, ensure_ascii=False, indent=2))

        return str(rpath), saved_json


# ============================================================================
# Test 3: Resource Factory slides resource entry
# ============================================================================


def test_resource_factory_entry() -> None:
    """Test the new Resource Factory entry point for session-cache-based slides.

    Uses ``generate_slides_resource_from_session_cache`` which wraps
    ``build_and_save_slides_from_session_cache`` with a resource-layer dict.
    """
    print()
    print("=" * 60)
    print("=== TEST 3: Resource Factory slides resource entry ===")
    print("=" * 60)

    session_id = "rf_test_session"

    outline = _make_outline_3nodes()

    with tempfile.TemporaryDirectory() as tmpdir:
        resource_root = Path(tmpdir)

        # -- Write handout caches (node_1, node_2 ready; node_3 no handout) --
        save_handout_cache_ready(
            session_id, "node_1",
            _make_handout_section_node_1(),
            source="rf_smoke_test",
            resource_root=resource_root,
        )
        save_handout_cache_ready(
            session_id, "node_2",
            _make_handout_section_node_2(),
            source="rf_smoke_test",
            resource_root=resource_root,
        )
        # node_3: intentionally no handout cache

        # -- Write tutor caches (node_1, node_2) --
        save_tutor_cache_ready(
            session_id, "node_1",
            _make_tutor_step_node_1(),
            source="rf_smoke_test",
            resource_root=resource_root,
        )
        save_tutor_cache_ready(
            session_id, "node_2",
            _make_tutor_step_node_2(),
            source="rf_smoke_test",
            resource_root=resource_root,
        )

        # -- Call the Resource Factory entry point --
        result = generate_slides_resource_from_session_cache(
            session_id=session_id,
            outline=outline,
            output_root=resource_root,
            deck_title="牙周炎基础学习",
            theme_id="clean_light",
        )

        # -- Assert return structure --
        _check(result["status"] == "ok", f"status == 'ok' (got {result['status']!r})")
        _check(result["resource_type"] == "slides_json",
               f"resource_type == 'slides_json' (got {result['resource_type']!r})")

        rpath = result["resource_path"]
        rpath_norm = rpath.replace("\\", "/")
        _check(rpath_norm.endswith("slides/slides.v1.json"),
               f"resource_path ends with slides/slides.v1.json: {rpath_norm}")

        # -- Assert file exists on disk --
        _check(os.path.isfile(rpath), f"file exists on disk: {rpath}")

        # -- Read resource_json --
        resource_json = result["resource_json"]
        _check(resource_json["schema_version"] == "slides.v1",
               "resource_json: schema_version == 'slides.v1'")
        _check(resource_json["deck_id"] == f"deck_{session_id}",
               f"resource_json: deck_id == 'deck_{session_id}'")
        _check(resource_json["theme_id"] == "clean_light",
               "resource_json: theme_id == 'clean_light'")
        _check(resource_json["deck_title"] == "牙周炎基础学习",
               "resource_json: deck_title from outline")

        saved_slides = resource_json["slides"]
        saved_warnings = result.get("warnings", [])

        # -- Slides count == valid handout cache count (2) --
        _check(len(saved_slides) == 2,
               f"slides count == 2 (got {len(saved_slides)})")

        # -- Slide order follows outline nodes --
        _check(saved_slides[0]["outline_node_id"] == "node_1",
               "slide[0] is node_1 (outline order)")
        _check(saved_slides[1]["outline_node_id"] == "node_2",
               "slide[1] is node_2 (outline order)")

        # -- Per-slide assertions --
        for slide in saved_slides:
            nid = slide["outline_node_id"]

            # slide.content does NOT contain transition_to_next
            _check("transition_to_next" not in slide["content"],
                   f"{nid}: slide.content does NOT contain transition_to_next")

            # Required content fields present
            for _f in ("overview", "key_points", "clinical_example", "common_confusions"):
                _check(_f in slide["content"], f"{nid}: slide.content.{_f} present")

            # Identifiers
            _check(slide["outline_node_id"] == slide["section_id"],
                   f"{nid}: outline_node_id == section_id")
            _check(slide["slide_id"] == f"slide_{nid}",
                   f"{nid}: slide_id == 'slide_{nid}'")

            # No transition block type
            slide_block_types = {b["block_type"] for b in slide["blocks"]}
            _check("transition" not in slide_block_types,
                   f"{nid}: blocks does NOT contain transition")

        # -- Full JSON string assertions: no transition_to_next --
        result_json_str = json.dumps(result, ensure_ascii=False)
        _check("transition_to_next" not in result_json_str,
               "result JSON string does NOT contain 'transition_to_next'")
        _check("了解了定义和病因后" not in result_json_str,
               "result JSON string does NOT contain node_1 transition text")
        _check("掌握分类后" not in result_json_str,
               "result JSON string does NOT contain node_2 transition text")

        # -- No legacy slides.json in session root --
        legacy_path = resource_root / session_id / "slides.json"
        _check(not legacy_path.exists(),
               f"no legacy slides.json in session root: {legacy_path}")

        # -- Warnings: node_3 missing handout --
        missing_h_warnings = [w for w in saved_warnings
                              if "handout cache status" in w and "node_3" in w]
        _check(len(missing_h_warnings) == 1,
               f"exactly 1 warning for node_3 missing handout cache: found {len(missing_h_warnings)}")

        # -- Warnings from resource_json should match result warnings --
        rj_warnings = resource_json.get("warnings", [])
        _check(len(rj_warnings) == len(saved_warnings),
               f"resource_json.warnings count ({len(rj_warnings)}) == result.warnings count ({len(saved_warnings)})")

        # -- Verify single slide cache files also exist (side effect of two-step) --
        slide_path_1 = resource_root / session_id / "slides" / "node_1.json"
        _check(slide_path_1.exists(),
               f"RF: single slide cache exists: {slide_path_1}")

        print()
        print("=== Resource Factory entry slides.v1.json ===")
        print(json.dumps(resource_json, ensure_ascii=False, indent=2))


# ============================================================================
# Test 4: optional handout modules
# ============================================================================


def test_optional_modules() -> None:
    """Verify that empty clinical_example and empty common_confusions produce
    no corresponding blocks, while the slide still generates correctly.

    Uses ``build_slides_from_handout_and_tutor`` — pure function, no LLM.
    """
    print()
    print("=" * 60)
    print("=== TEST 4: optional handout modules ===")
    print("=" * 60)

    session_id = "optional_test"

    # -- Fixture: handout section with empty clinical_example + empty common_confusions --
    section_empty_optionals: dict = {
        "outline_node_id": "node_1",
        "title": "仅含概述与要点的章节",
        "section_status": "llm_generated",
        "evidence_status": "sufficient",
        "used_evidence": {
            "kg_node_ids": ["kg://perio/001"],
            "kg_edge_ids": [],
            "concept_ids": [],
            "question_ids": [],
            "evidence_chunk_ids": ["chunk_x", "chunk_y"],
        },
        "content": {
            "overview": "本节介绍牙周炎的基本概念和核心病因机制。",
            "key_points": [
                {
                    "heading": "牙周炎的定义",
                    "explanation": "牙周炎是一种累及牙周支持组织的慢性感染性疾病。",
                    "evidence_refs": ["chunk_x"],
                },
                {
                    "heading": "菌斑生物膜的致病机制",
                    "explanation": "菌斑生物膜是牙周炎最主要的始动因子，通过细菌及其产物引发宿主炎症反应。",
                    "evidence_refs": ["chunk_y"],
                },
            ],
            "clinical_example": "",
            "common_confusions": [],
            "transition_to_next": "理解了基本概念后，下一节将介绍牙周炎的临床表现与分类。",
        },
        "missing_evidence": [],
    }

    # -- outline (single node) --
    outline = {
        "title": "牙周炎基础概念",
        "nodes": [
            {
                "id": "node_1",
                "title": "牙周炎的基本概念",
                "learning_objectives": ["理解牙周炎的定义和病因"],
                "sequence_reason": "从基础概念开始",
                "kg_node_ids": ["kg://perio/001"],
            },
        ],
    }

    handout_sections = {"node_1": section_empty_optionals}
    tutor_steps: dict[str, dict] = {}

    result = build_slides_from_handout_and_tutor(
        session_id=session_id,
        outline=outline,
        handout_sections_by_node_id=handout_sections,
        tutor_steps_by_node_id=tutor_steps,
        deck_title="牙周炎基础概念",
    )

    slides = result["slides"]
    warnings = result.get("warnings", [])

    # -- Slides count = 1 --
    _check(len(slides) == 1, f"slides count == 1 (got {len(slides)})")

    slide = slides[0]
    node_id = slide["outline_node_id"]

    # -- Slide identifiers --
    _check(slide["outline_node_id"] == "node_1", f"{node_id}: outline_node_id == node_1")
    _check(slide["outline_node_id"] == slide["section_id"], f"{node_id}: outline_node_id == section_id")
    _check(slide["slide_id"] == "slide_node_1", f"{node_id}: slide_id")
    _check(slide["slide_type"] == "mixed_learning", f"{node_id}: slide_type")
    _check(slide["title"] == "仅含概述与要点的章节", f"{node_id}: title from handout section")

    # -- slide.content does NOT contain transition_to_next --
    _check("transition_to_next" not in slide["content"],
           f"{node_id}: content does NOT contain transition_to_next")

    # -- slide.content still projects the optional fields (with original empty values) --
    _check("clinical_example" in slide["content"],
           f"{node_id}: content.clinical_example field present")
    _check(slide["content"]["clinical_example"] == "",
           f"{node_id}: content.clinical_example == ''")

    _check("common_confusions" in slide["content"],
           f"{node_id}: content.common_confusions field present")
    _check(slide["content"]["common_confusions"] == [],
           f"{node_id}: content.common_confusions == []")

    # -- Required content fields present --
    _check(slide["content"]["overview"] != "", f"{node_id}: content.overview non-empty")
    _check(isinstance(slide["content"]["key_points"], list) and len(slide["content"]["key_points"]) == 2,
           f"{node_id}: content.key_points has 2 items")

    # -- Block type assertions: overview + key_point present; clinical_example/confusion absent --
    block_types = {b["block_type"] for b in slide["blocks"]}
    _check("overview" in block_types, f"{node_id}: blocks contains overview")
    _check("key_point" in block_types, f"{node_id}: blocks contains key_point")
    _check("clinical_example" not in block_types,
           f"{node_id}: blocks does NOT contain clinical_example (input was empty)")
    _check("confusion" not in block_types,
           f"{node_id}: blocks does NOT contain confusion (input was empty)")
    _check("transition" not in block_types,
           f"{node_id}: blocks does NOT contain transition")

    # -- Blocks count: 1 overview + 2 key_points = 3 --
    _check(len(slide["blocks"]) == 3,
           f"{node_id}: blocks count == 3 (got {len(slide['blocks'])})")

    # -- Verify each key_point block --
    kp_blocks = [b for b in slide["blocks"] if b["block_type"] == "key_point"]
    _check(len(kp_blocks) == 2, f"{node_id}: 2 key_point blocks (got {len(kp_blocks)})")
    for kpb in kp_blocks:
        _check(kpb.get("semantic_role") == "concept",
               f"{node_id}: key_point semantic_role='concept'")
        _check(isinstance(kpb.get("evidence_refs"), list),
               f"{node_id}: key_point evidence_refs list")

    # -- Verify overview block --
    overview_blocks = [b for b in slide["blocks"] if b["block_type"] == "overview"]
    _check(len(overview_blocks) == 1,
           f"{node_id}: 1 overview block (got {len(overview_blocks)})")

    # -- JSON string assertions: no transition_to_next or transition text anywhere --
    result_json_str = json.dumps(result, ensure_ascii=False)
    _check("transition_to_next" not in result_json_str,
           "result JSON string does NOT contain 'transition_to_next'")
    _check("理解了基本概念后" not in result_json_str,
           "result JSON string does NOT contain transition text")

    # -- Input fixture still has transition_to_next (prove no mutation) --
    _check("transition_to_next" in section_empty_optionals["content"],
           "input fixture still has transition_to_next")

    # -- Bullets from key_points --
    bullets = slide["bullets"]
    _check(isinstance(bullets, list) and len(bullets) == 2,
           f"{node_id}: bullets count == 2 (got {len(bullets)})")
    _check(bullets[0] == "牙周炎的定义", f"{node_id}: bullet[0] heading")
    _check(bullets[1] == "菌斑生物膜的致病机制", f"{node_id}: bullet[1] heading")

    # -- Warning: no tutor_step for node_1 --
    tutor_warnings = [w for w in warnings if "tutor_step missing" in w]
    _check(len(tutor_warnings) == 1,
           f"1 warning for missing tutor_step: found {len(tutor_warnings)}")

    print()
    print("=== TEST 4 slide JSON ===")
    print(json.dumps(slide, ensure_ascii=False, indent=2))


# ============================================================================
# Test 5: build_slide_from_handout_section — single slide pure builder
# ============================================================================


def test_single_slide_pure_builder() -> None:
    """Verify ``build_slide_from_handout_section`` independently generates
    a single slide without tutor_step (tutor missing → warning, no block).
    """
    print()
    print("=" * 60)
    print("=== TEST 5: build_slide_from_handout_section (single slide pure builder) ===")
    print("=" * 60)

    outline_node = {
        "id": "node_1",
        "title": "牙周炎的定义与病因",
    }
    handout_section = _make_handout_section_node_1()

    # --- Test A: without tutor_step ---
    result = build_slide_from_handout_section(
        session_id="test_single",
        outline_node=outline_node,
        handout_section=handout_section,
        tutor_step=None,
    )

    slide = result["slide"]
    warnings = result["warnings"]

    # Warnings
    tutor_warnings = [w for w in warnings if "tutor_step missing" in w]
    _check(len(tutor_warnings) == 1,
           f"tutor_step missing → 1 warning: found {len(tutor_warnings)}")

    # Slide structure
    _check(slide["outline_node_id"] == "node_1", "A: outline_node_id == node_1")
    _check(slide["section_id"] == "node_1", "A: section_id == node_1")
    _check(slide["slide_id"] == "slide_node_1", "A: slide_id")
    _check(slide["slide_type"] == "mixed_learning", "A: slide_type")
    _check(slide["title"] == "牙周炎的定义与病因", "A: title from handout section")

    # narration_ref / quiz_ref still point to node even without tutor_step
    _check(slide["narration_ref"]["source"] == "tutor_step.teaching_text",
           "A: narration_ref.source")
    _check(slide["narration_ref"]["outline_node_id"] == "node_1",
           "A: narration_ref.outline_node_id")
    _check(slide["quiz_ref"]["source"] == "tutor_step.questions",
           "A: quiz_ref.source")
    _check(slide["quiz_ref"]["outline_node_id"] == "node_1",
           "A: quiz_ref.outline_node_id")

    # No transition_to_next in output
    _check("transition_to_next" not in slide["content"],
           "A: content does NOT contain transition_to_next")

    # Blocks present
    _check(len(slide["blocks"]) > 0, f"A: blocks non-empty ({len(slide['blocks'])})")
    block_types = {b["block_type"] for b in slide["blocks"]}
    _check("overview" in block_types, "A: blocks contains overview")
    _check("key_point" in block_types, "A: blocks contains key_point")
    _check("clinical_example" in block_types, "A: blocks contains clinical_example")
    _check("confusion" in block_types, "A: blocks contains confusion")
    _check("transition" not in block_types, "A: blocks does NOT contain transition")

    # --- Test B: with tutor_step ---
    result_b = build_slide_from_handout_section(
        session_id="test_single",
        outline_node=outline_node,
        handout_section=handout_section,
        tutor_step=_make_tutor_step_node_1(),
    )

    slide_b = result_b["slide"]
    warnings_b = result_b["warnings"]

    # No "tutor_step missing" warning
    tutor_missing_b = [w for w in warnings_b if "tutor_step missing" in w]
    _check(len(tutor_missing_b) == 0,
           f"B: with tutor_step → no 'tutor_step missing' warning: found {len(tutor_missing_b)}")

    # Slide still valid
    _check(slide_b["outline_node_id"] == "node_1", "B: outline_node_id == node_1")
    _check("transition_to_next" not in slide_b["content"],
           "B: content does NOT contain transition_to_next")

    print()
    print("=== TEST 5 single slide JSON (without tutor_step) ===")
    print(json.dumps(slide, ensure_ascii=False, indent=2))


# ============================================================================
# Test 6: build_and_save_slide_for_node_from_session_cache — single node cache
# ============================================================================


def test_single_node_slide_cache() -> None:
    """Verify a single slide is saved to ``slides/{node_id}.json`` and the
    resource-layer return dict is correct.
    """
    print()
    print("=" * 60)
    print("=== TEST 6: build_and_save_slide_for_node_from_session_cache ===")
    print("=" * 60)

    session_id = "single_node_test"
    outline = _make_outline_3nodes()

    with tempfile.TemporaryDirectory() as tmpdir:
        resource_root = Path(tmpdir)

        # Write handout + tutor caches for node_1 only
        save_handout_cache_ready(
            session_id, "node_1",
            _make_handout_section_node_1(),
            source="single_node_test",
            resource_root=resource_root,
        )
        save_tutor_cache_ready(
            session_id, "node_1",
            _make_tutor_step_node_1(),
            source="single_node_test",
            resource_root=resource_root,
        )

        # --- Call single-node builder ---
        result = build_and_save_slide_for_node_from_session_cache(
            session_id=session_id,
            outline=outline,
            outline_node_id="node_1",
            resource_root=resource_root,
        )

        # --- Assert return structure ---
        _check(result["status"] == "ok", f"status == 'ok' (got {result['status']!r})")
        _check(result["resource_type"] == "single_slide_json",
               f"resource_type == 'single_slide_json' (got {result['resource_type']!r})")

        rpath = result["resource_path"]
        rpath_norm = rpath.replace("\\", "/")
        _check(rpath_norm.endswith("slides/node_1.json"),
               f"resource_path ends with slides/node_1.json: {rpath_norm}")

        # --- Assert file exists on disk ---
        _check(os.path.isfile(rpath), f"single slide file exists: {rpath}")

        # --- Read saved slide JSON ---
        with open(rpath, "r", encoding="utf-8") as f:
            saved_slide = json.load(f)

        _check(saved_slide["outline_node_id"] == "node_1",
               "saved single slide: outline_node_id == node_1")
        _check(saved_slide["section_id"] == "node_1",
               "saved single slide: section_id == node_1")
        _check(saved_slide["slide_id"] == "slide_node_1",
               "saved single slide: slide_id")
        _check(saved_slide["title"] == "牙周炎的定义与病因",
               "saved single slide: title")

        # No transition_to_next in saved slide
        saved_json_str = json.dumps(saved_slide, ensure_ascii=False)
        _check("transition_to_next" not in saved_json_str,
               "saved single slide: no transition_to_next")

        # No legacy slides.json
        legacy_path = resource_root / session_id / "slides.json"
        _check(not legacy_path.exists(), "no legacy slides.json")

        # resource_json matches saved file
        _check(result["resource_json"] == saved_slide,
               "resource_json matches saved file content")

        print()
        print("=== TEST 6 single slide JSON ===")
        print(json.dumps(saved_slide, ensure_ascii=False, indent=2))


# ============================================================================
# Test 7: aggregate_slides_from_node_cache — partial nodes, ordering, warnings
# ============================================================================


def test_aggregate_partial_nodes() -> None:
    """Verify ``aggregate_slides_from_node_cache``:
    - Only existing slides in deck.
    - Deck order follows outline order.
    - Missing nodes produce warnings, don't block.
    - Aggregator does NOT re-generate missing slides.
    """
    print()
    print("=" * 60)
    print("=== TEST 7: aggregate_slides_from_node_cache (partial nodes) ===")
    print("=" * 60)

    session_id = "aggregate_test"
    outline = _make_outline_3nodes()

    with tempfile.TemporaryDirectory() as tmpdir:
        resource_root = Path(tmpdir)
        slides_dir = resource_root / session_id / "slides"
        slides_dir.mkdir(parents=True, exist_ok=True)

        # Only write single slide caches for node_1 and node_3 (skip node_2)
        # This simulates "node 1 ready, node 2 not yet, node 3 somehow ready early"
        slide_node_1 = {
            "slide_id": "slide_node_1",
            "outline_node_id": "node_1",
            "section_id": "node_1",
            "slide_type": "mixed_learning",
            "title": "牙周炎的定义与病因",
            "content": {"overview": "test", "key_points": [], "clinical_example": "", "common_confusions": []},
            "blocks": [],
            "bullets": [],
            "tutor_step_ref": "node_1",
            "narration_ref": {"source": "tutor_step.teaching_text", "outline_node_id": "node_1"},
            "quiz_ref": {"source": "tutor_step.questions", "outline_node_id": "node_1"},
            "related_kg_node_ids": [],
            "visual_hint": {"diagram_type": "mixed_learning_card"},
            "subtitle": "",
        }
        slide_node_3 = {
            "slide_id": "slide_node_3",
            "outline_node_id": "node_3",
            "section_id": "node_3",
            "slide_type": "mixed_learning",
            "title": "牙周炎的治疗原则",
            "content": {"overview": "test3", "key_points": [], "clinical_example": "", "common_confusions": []},
            "blocks": [],
            "bullets": [],
            "tutor_step_ref": "node_3",
            "narration_ref": {"source": "tutor_step.teaching_text", "outline_node_id": "node_3"},
            "quiz_ref": {"source": "tutor_step.questions", "outline_node_id": "node_3"},
            "related_kg_node_ids": [],
            "visual_hint": {"diagram_type": "mixed_learning_card"},
            "subtitle": "",
        }

        (slides_dir / "node_1.json").write_text(
            json.dumps(slide_node_1, ensure_ascii=False, indent=2), encoding="utf-8")
        (slides_dir / "node_3.json").write_text(
            json.dumps(slide_node_3, ensure_ascii=False, indent=2), encoding="utf-8")
        # node_2.json intentionally absent

        # --- Call aggregator ---
        deck = aggregate_slides_from_node_cache(
            session_id=session_id,
            outline=outline,
            resource_root=resource_root,
            deck_title="牙周炎基础学习",
        )

        # --- Deck schema ---
        _check(deck["schema_version"] == "slides.v1", "deck: schema_version == 'slides.v1'")
        _check(deck["deck_id"] == f"deck_{session_id}", f"deck: deck_id")
        _check(deck["session_id"] == session_id, "deck: session_id")
        _check(deck["deck_title"] == "牙周炎基础学习", "deck: deck_title")
        _check(deck["source"]["aggregated_from"] == "single_slide_cache",
               "deck: source.aggregated_from == 'single_slide_cache'")

        slides = deck["slides"]
        deck_warnings = deck.get("warnings", [])

        # --- Only node_1 and node_3 in deck ---
        _check(len(slides) == 2,
               f"deck.slides count == 2 (node_2 missing): got {len(slides)}")

        # --- Order follows outline: node_1, node_3 ---
        _check(slides[0]["outline_node_id"] == "node_1", "deck slide[0] is node_1 (outline order)")
        _check(slides[1]["outline_node_id"] == "node_3", "deck slide[1] is node_3 (outline order)")

        # --- node_2 produces missing_slide_cache warning ---
        missing_warnings = [w for w in deck_warnings if "missing_slide_cache" in w and "node_2" in w]
        _check(len(missing_warnings) >= 1,
               f"missing_slide_cache warning for node_2: found {len(missing_warnings)}")

        # --- node_1 and node_3 do NOT produce missing_slide_cache warnings ---
        for nid in ("node_1", "node_3"):
            false_warnings = [w for w in deck_warnings
                            if "missing_slide_cache" in w and nid in w
                            and w.startswith("missing_slide_cache")]
            _check(len(false_warnings) == 0,
                   f"no missing_slide_cache warning for {nid}: found {len(false_warnings)}")

        # --- Aggregator does NOT create missing files ---
        _check(not (slides_dir / "node_2.json").exists(),
               "aggregator did NOT create node_2.json (does not force generation)")

        # --- No legacy slides.json ---
        legacy_path = resource_root / session_id / "slides.json"
        _check(not legacy_path.exists(), "no legacy slides.json")

        print()
        print("=== TEST 7 aggregated deck JSON ===")
        print(json.dumps(deck, ensure_ascii=False, indent=2))


# ============================================================================
# Test 8: tutor_step missing — does not block single slide generation
# ============================================================================


def test_tutor_step_missing_no_block() -> None:
    """Verify that when tutor_step cache is missing, the single slide is still
    generated, with narration_ref/quiz_ref pointing to the node_id and a warning.
    """
    print()
    print("=" * 60)
    print("=== TEST 8: tutor_step missing — no block on single slide ===")
    print("=" * 60)

    session_id = "tutor_missing_test"
    outline = _make_outline_3nodes()

    with tempfile.TemporaryDirectory() as tmpdir:
        resource_root = Path(tmpdir)

        # Write handout cache only — NO tutor cache for node_1
        save_handout_cache_ready(
            session_id, "node_1",
            _make_handout_section_node_1(),
            source="tutor_missing_test",
            resource_root=resource_root,
        )
        # Intentionally NO tutor cache for node_1

        # --- Call single-node builder ---
        result = build_and_save_slide_for_node_from_session_cache(
            session_id=session_id,
            outline=outline,
            outline_node_id="node_1",
            resource_root=resource_root,
        )

        # --- Slide was generated successfully ---
        _check(result["status"] == "ok",
               f"status == 'ok' despite missing tutor_step (got {result['status']!r})")

        slide = result["resource_json"]
        _check(slide["outline_node_id"] == "node_1",
               "slide.outline_node_id == node_1")
        _check(slide["title"] == "牙周炎的定义与病因",
               "slide.title from handout")

        # narration_ref and quiz_ref still point to node
        _check(slide["narration_ref"]["outline_node_id"] == "node_1",
               "narration_ref.outline_node_id == node_1 (tutor missing)")
        _check(slide["quiz_ref"]["outline_node_id"] == "node_1",
               "quiz_ref.outline_node_id == node_1 (tutor missing)")

        # Warnings: tutor cache status warning from save function + tutor_step missing from builder
        tutor_warnings = [w for w in result["warnings"]
                         if "tutor" in w.lower()]
        _check(len(tutor_warnings) >= 1,
               f"tutor-related warnings: found {len(tutor_warnings)}")

        # --- File saved to disk ---
        _check(os.path.isfile(result["resource_path"]),
               f"single slide file exists: {result['resource_path']}")

        print()
        print("=== TEST 8 single slide JSON (tutor_step missing) ===")
        print(json.dumps(slide, ensure_ascii=False, indent=2))


# ============================================================================
# Test 9: two-step flow — single slide caches feed aggregator
# ============================================================================


def test_two_step_flow() -> None:
    """End-to-end verification: build_and_save_slides_from_session_cache creates
    single slide caches, then the aggregator reads them back to form the deck.
    """
    print()
    print("=" * 60)
    print("=== TEST 9: two-step flow — single slides → aggregated deck ===")
    print("=" * 60)

    session_id = "twostep_test"
    outline = _make_outline_3nodes()

    with tempfile.TemporaryDirectory() as tmpdir:
        resource_root = Path(tmpdir)

        # Write handout + tutor caches for node_1 and node_2
        save_handout_cache_ready(
            session_id, "node_1",
            _make_handout_section_node_1(),
            source="twostep_test",
            resource_root=resource_root,
        )
        save_handout_cache_ready(
            session_id, "node_2",
            _make_handout_section_node_2(),
            source="twostep_test",
            resource_root=resource_root,
        )
        save_tutor_cache_ready(
            session_id, "node_1",
            _make_tutor_step_node_1(),
            source="twostep_test",
            resource_root=resource_root,
        )
        save_tutor_cache_ready(
            session_id, "node_2",
            _make_tutor_step_node_2(),
            source="twostep_test",
            resource_root=resource_root,
        )

        # --- Step 1+2: build and save (creates single slides + deck) ---
        result = build_and_save_slides_from_session_cache(
            session_id=session_id,
            outline=outline,
            resource_root=resource_root,
        )

        _check(result["status"] == "ok", f"two-step: status == 'ok'")

        # --- Single slide caches exist ---
        for nid in ("node_1", "node_2"):
            sp = resource_root / session_id / "slides" / f"{nid}.json"
            _check(sp.exists(), f"two-step: single slide cache exists: {sp}")

        # --- Deck exists ---
        deck_path = resource_root / session_id / "slides" / "slides.v1.json"
        _check(deck_path.exists(), f"two-step: deck exists: {deck_path}")

        # --- Deck content matches single slides ---
        with open(deck_path, "r", encoding="utf-8") as f:
            deck = json.load(f)

        deck_slides = deck["slides"]
        _check(len(deck_slides) == 2, f"two-step: deck has 2 slides (got {len(deck_slides)})")

        # Read single slides and compare
        for i, nid in enumerate(("node_1", "node_2")):
            sp = resource_root / session_id / "slides" / f"{nid}.json"
            with open(sp, "r", encoding="utf-8") as f:
                single = json.load(f)
            # Compare key fields (single slide is a subset of the deck entry)
            for key in ("slide_id", "outline_node_id", "section_id", "title"):
                _check(deck_slides[i].get(key) == single.get(key),
                       f"two-step: deck.slides[{i}].{key} matches single slide {nid}.{key}")

        # --- deck.source has aggregated_from ---
        _check(deck["source"].get("aggregated_from") == "single_slide_cache",
               "two-step: deck.source.aggregated_from == 'single_slide_cache'")

        # --- No legacy slides.json ---
        legacy_path = resource_root / session_id / "slides.json"
        _check(not legacy_path.exists(), "two-step: no legacy slides.json")


# ============================================================================
# Test 10: empty common_confusions — no confusion blocks in single slide cache
# ============================================================================


def test_optional_modules_single_slide() -> None:
    """Verify empty clinical_example / common_confusions don't produce blocks
    when building through the single-slide cache path.
    """
    print()
    print("=" * 60)
    print("=== TEST 10: optional modules via single slide cache ===")
    print("=" * 60)

    session_id = "optional_single_test"

    section_no_optionals = {
        "outline_node_id": "node_1",
        "title": "仅概述与要点",
        "section_status": "llm_generated",
        "evidence_status": "sufficient",
        "used_evidence": {
            "kg_node_ids": [],
            "kg_edge_ids": [],
            "concept_ids": [],
            "question_ids": [],
            "evidence_chunk_ids": [],
        },
        "content": {
            "overview": "概述内容。",
            "key_points": [
                {"heading": "要点1", "explanation": "解释1", "evidence_refs": []},
            ],
            "clinical_example": "",
            "common_confusions": [],
            "transition_to_next": "过渡句不应出现。",
        },
        "missing_evidence": [],
    }

    outline = {
        "title": "测试",
        "nodes": [{"id": "node_1", "title": "测试节点"}],
    }

    with tempfile.TemporaryDirectory() as tmpdir:
        resource_root = Path(tmpdir)

        save_handout_cache_ready(
            session_id, "node_1",
            section_no_optionals,
            source="optional_single_test",
            resource_root=resource_root,
        )

        result = build_and_save_slide_for_node_from_session_cache(
            session_id=session_id,
            outline=outline,
            outline_node_id="node_1",
            resource_root=resource_root,
        )

        _check(result["status"] == "ok", f"status ok (got {result['status']!r})")

        slide = result["resource_json"]
        block_types = {b["block_type"] for b in slide["blocks"]}

        _check("overview" in block_types, "blocks contains overview")
        _check("key_point" in block_types, "blocks contains key_point")
        _check("clinical_example" not in block_types,
               "blocks does NOT contain clinical_example (empty)")
        _check("confusion" not in block_types,
               "blocks does NOT contain confusion (empty)")
        _check("transition" not in block_types,
               "blocks does NOT contain transition")

        # No transition_to_next in saved output
        slide_json_str = json.dumps(slide, ensure_ascii=False)
        _check("transition_to_next" not in slide_json_str,
               "single slide: no transition_to_next")


# ============================================================================
# Main
# ============================================================================


def main() -> None:
    global _FAILED
    _FAILED = 0

    # Test 1: pure function
    test_pure_function()

    # Test 2: session cache (now also verifies single slide cache files)
    saved_path, resource_json = test_session_cache()

    # Test 3: Resource Factory slides resource entry
    test_resource_factory_entry()

    # Test 4: optional handout modules
    test_optional_modules()

    # Test 5: single slide pure builder (new)
    test_single_slide_pure_builder()

    # Test 6: single node slide cache save (new)
    test_single_node_slide_cache()

    # Test 7: aggregation with partial nodes (new)
    test_aggregate_partial_nodes()

    # Test 8: tutor_step missing does not block (new)
    test_tutor_step_missing_no_block()

    # Test 9: two-step flow end-to-end (new)
    test_two_step_flow()

    # Test 10: optional modules via single slide cache (new)
    test_optional_modules_single_slide()

    # -- Final result ---------------------------------------------------------
    print()
    if _FAILED == 0:
        print("learning slides smoke test passed")
    else:
        print(f"learning slides smoke test FAILED: {_FAILED} assertion(s)")
        sys.exit(1)


if __name__ == "__main__":
    main()
