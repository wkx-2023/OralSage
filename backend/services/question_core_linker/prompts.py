from __future__ import annotations

import json
from typing import Any

from backend.services.question_bank_builder.qwen_client import schema_instruction

from .schemas import ChapterRouteDecision, CoreMatchLLMDecision, CoreVerifierDecision


NO_FALLBACK_SYSTEM = """你是口腔医学题库与知识图谱对齐审查员。

硬性规则：
1. 只能根据用户提供的题库记录、KG 章节候选、KG core/support/concept 候选判断。
2. 不允许使用外部知识补全 KG 中不存在的 core。
3. 不允许跨学科猜测；候选外的 core 一律不能选择。
4. 证据不足时输出 ambiguous、no_match、reject 或 uncertain，不要强行匹配。
5. 严格输出 JSON object，不输出 Markdown。"""


def record_payload(record: dict[str, Any]) -> dict[str, Any]:
    provisional_core = record.get("provisional_core") or {}
    return {
        "id": record.get("id"),
        "subject_name": record.get("subject_name"),
        "chapter_name": record.get("chapter_name"),
        "local_question_no": record.get("local_question_no"),
        "source_exam_id": record.get("source_exam_id"),
        "stem": record.get("stem"),
        "options": record.get("options"),
        "answer": record.get("answer"),
        "explanation": record.get("explanation"),
        "primary_knowledge_point": record.get("primary_knowledge_point"),
        "knowledge_points": record.get("knowledge_points"),
        "provisional_core": {
            "type": provisional_core.get("type"),
            "canonical_name": provisional_core.get("canonical_name"),
            "aliases": provisional_core.get("aliases"),
            "rationale": provisional_core.get("rationale"),
            "supporting_terms": provisional_core.get("supporting_terms"),
        },
    }


def chapter_route_prompt(
    *,
    source_subject_name: str,
    source_chapter_name: str,
    candidates: list[dict[str, Any]],
) -> str:
    return f"""任务：在同名学科内判断题库章节应对应哪个 KG 章节。

约束：
1. 只允许从 candidate_chapters 中选择。
2. 只处理章节名细微差异、同义写法、少量增删字、术语异写。
3. 如果候选中没有明确对应章节，decision 输出 no_match。
4. 如果多个候选都可能且无法唯一判断，decision 输出 ambiguous。
5. 不要因为主题看起来相关就跨章硬配。

输出 JSON 必须符合 schema：
{schema_instruction(ChapterRouteDecision)}

source_subject_name:
{source_subject_name}

source_chapter_name:
{source_chapter_name}

candidate_chapters:
{json.dumps(candidates, ensure_ascii=False, indent=2)}
"""


def core_match_prompt(
    *,
    record: dict[str, Any],
    candidate_cores: list[dict[str, Any]],
) -> str:
    return f"""任务：把一道已清洗题库记录匹配到 KG 中“最直接命中的知识节点”，再回挂到正式 KG core。

匹配逻辑：
1. 先判断题目真正考查的核心知识点，不要只看词面相似。
2. 先在 candidate_cores 及其 support_nodes、concept_nodes 中选择最直接知识节点，写入 matched_node。
3. 最终 primary_core_uid 必须来自 candidate_cores 的 kg_core_uid，且 matched_node.parent_core_uid 必须等于 primary_core_uid。
4. 如果题目直接考 core_node，match_type = direct_core，matched_node.node_kind = core_node，parent_link_strength = direct。
5. 如果题目直接考 support_node，match_type = support_to_core，matched_node.node_kind = support_node，parent_link_strength = edge。
   - support_node 是 core 下的细粒度知识节点；只要 support_node 精准承载题目主考点，并且 parent_edges 或候选包证明它属于该 core，就可以回挂到 primary_core。
   - 不要因为 primary_core 是 support_node 的上位主题就判为“过宽”；这种上位关系是 support_to_core 的正常结构。
   - 如果 parent_edges 中出现 edge_direction_suspect，只把它作为 KG 质量风险写入 rationale/risk_flags；若 evidence 明确说明 support 是该 core 的子类/组成部分，不得仅因边方向疑点判 no_match。
6. 如果题目直接考 concept_node，match_type = concept_to_core，matched_node.node_kind = concept_node。
7. concept_node 需要判断其回挂到 parent core 的强度：
   - strong_related：concept 语义确实属于该 core 的知识范围，可作为匹配。
   - weak_related：KG 只是弱关联或挂靠可疑，仍可输出 match，但 verifier 可能判为 review_required。
   - invalid：不能回挂该 core，应输出 no_match。
8. 如果题目同时依赖 support 和 concept，match_type = mixed，并在 bridge_nodes 中列出相关节点。
9. 如果候选节点都过宽、证据不足或没有明确所属 core，decision = no_match。
10. 如果多个节点均合理且无法唯一确定，decision = ambiguous。
11. 不允许选择候选外 core/support/concept，不允许发明节点。

matched_node 字段要求：
- node_kind 必须是 core_node、support_node 或 concept_node。
- client_ref 必须来自候选 KG 节点。
- canonical_name 必须与候选节点名称一致。
- parent_core_uid/name/type 必须来自该候选节点所在的 core。
- parent_link_reason 必须说明为什么该节点可以或不能回挂到 parent core。

输出 JSON 必须符合 schema：
{schema_instruction(CoreMatchLLMDecision)}

题库记录：
{json.dumps(record_payload(record), ensure_ascii=False, indent=2)}

candidate_cores：
{json.dumps(candidate_cores, ensure_ascii=False, indent=2)}
"""


def verifier_prompt(
    *,
    record: dict[str, Any],
    kg_core_linking: dict[str, Any],
    selected_core: dict[str, Any] | None,
) -> str:
    return f"""任务：复核一条题库到 KG core 的匹配是否可以接受。

accept 条件：
1. primary_core_uid 来自候选 KG core。
2. matched_knowledge_node 是题目最直接考查的 KG 节点。
3. 题目主考点与 primary_core 的范围一致，或可通过 matched_knowledge_node/bridge_nodes 明确挂回该 core。
4. support_node 必须有明确 edge 或候选包中明确列在该 core 下。
5. 对 support_to_core：如果题目主考点由 support_node 精准承载，且该 support_node 通过 parent_edges 或候选包明确属于 primary_core，则应 accept；不得因为 primary_core 是 support_node 的上位主题而 reject。
6. 对 support_to_core：如果 parent_edges 有 edge_direction_suspect，但 evidence/support_explanation 明确说明 support 是 core 的子类、类型、组成部分、并发症或方法，则仍可 accept，并在 risk_flags 中加入 edge_direction_suspect。
7. concept_node 必须不是纯弱关联；如果 parent_link_strength 是 weak_related 且 core 名称明显不承载该 concept，必须 reject 或 uncertain。
8. 没有跨学科、跨章节、只靠词面相似、无 support/concept 证据的过宽泛匹配问题。
9. rationale 与题干/答案/解析一致。

reject 条件：
1. 所选 core 与题目主考点不一致。
2. matched_knowledge_node 或 bridge_nodes 桥接不成立。
3. 匹配明显过宽泛，且没有精准 support_node/concept_node 作为桥接证据。
4. 只因为出现相同词语而匹配。
5. support_node 只对应题干背景词，但题目答案真正考查的是 KG 中不存在的并发症、剂量、数值比较、禁忌证、鉴别点等独立知识点。

uncertain 条件：
1. 可能正确但证据不足。
2. 多个 core 难以区分。
3. 需要人工审查。

输出 JSON 必须符合 schema：
{schema_instruction(CoreVerifierDecision)}

题库记录：
{json.dumps(record_payload(record), ensure_ascii=False, indent=2)}

待复核匹配：
{json.dumps(kg_core_linking, ensure_ascii=False, indent=2)}

selected_core：
{json.dumps(selected_core, ensure_ascii=False, indent=2)}
"""
