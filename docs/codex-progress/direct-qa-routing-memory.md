# Direct QA 路由、记忆与画像进度

## 已独立验证

### 阶段 1：数据来源与迁移架构冻结（通过，2026-07-14）

- `mastery_records.session_id` 和 `lesson_events.session_id` 都是 `NOT NULL`；Direct QA 不得写入或伪造课程 session。
- `update_profile_from_qa()` 是无 session 依赖的纯函数，可由后续 Direct QA adapter 复用。
- SQLite 不能通过 `ALTER TABLE` 放宽既有 `NOT NULL` 约束；重建 `mastery_records` 会触及课程 mastery 的十余处读写/统计边界，触发计划停止条件。
- 已冻结最小方案：新增物理隔离的 `direct_qa_mastery_records` 与 LearningMemory 专用读写接口，保存 `user_id`、KG 节点、掌握度、`source=direct_qa`、`source_ref` 和 evidence。课程表、session summary、Tutor 和 study plan 的既有 session 查询保持不变。

证据：`backend/services/learning/memory.py` schema、`upsert_mastery()`、`get_mastery_by_kg_node()`、`session_summary()`，以及 `orchestrator.py` 与 `study_plan.py` 的调用点；阶段 1 返工任务 `task-20260714-221449` 无产品文件改动、无提交，Codex 独立复查通过。

## 当前需求账本

| Requirement | 状态 | 证据 |
| --- | --- | --- |
| Direct QA 不伪造课程 session 的数据边界 | designed | 阶段 1 schema/消费者核查 |
| Direct QA 专用 mastery 持久化 | verified | 阶段 2 的隔离表读写测试通过；尚未由 Direct QA 服务调用 |
| 困惑时更新薄弱点与 mastery | verified | 阶段 3 adapter 测试证明严格 `True` + 非空 ID 才写入；尚未完成服务端有效 KG 关联与调用 |
| Direct QA 服务的 KAG 回答与追问重取证 | verified | 阶段 4 服务层 77 项组合测试通过；尚未接入持久化、Router 或前端 |
| 前端直接问答与追问 | cannot-prove | 尚未实现 |

## 下一最小任务

### 阶段 2：Direct QA mastery 基础（通过，2026-07-14）

- `memory.py` 新增物理隔离的 `direct_qa_mastery_records`、按 user/KG/concept upsert 和按用户读取接口；不修改课程 `mastery_records`、`upsert_mastery()` 或 session 查询。
- 独立复跑：`python -m pytest tests/test_direct_qa_mastery.py -q`，21 passed。
- 已验证：首次/重复更新、分数/置信度夹紧、用户和 KG 隔离、source/evidence/source_ref 保留，以及课程 mastery 不被污染。

## 下一最小任务

### 阶段 3：Direct QA 画像适配器（通过，2026-07-14）

- 新增 `backend/services/qa/direct_qa_persistence.py`，仅对严格 `should_reinforce is True` 且有效去重 KG 节点更新画像和 Direct QA mastery；每节点 delta 为 `-0.03`。
- 不写课程 mastery、lesson event 或 learning session；普通/无效信号不写入。
- 独立复跑：`python -m pytest tests/test_direct_qa_mastery.py tests/test_direct_qa_persistence.py -q`，48 passed。

## 下一最小任务

### 阶段 4：Direct QA 服务的 KAG 回答与追问重取证（通过，2026-07-14）

- 新增 `backend/services/qa/direct_qa.py`：输入 `message + history + user_id`，每轮均先让 LLM 消歧/生成检索查询，再调用 KAG、以本轮证据生成纯文字回答；历史只传给消歧阶段，不作为回答证据。
- 结果不含 session、outline 或 PPT；未导入 orchestrator、`start_lesson` 或 `handle_interruption`。
- 返工 `task-20260714-231842` 修复空 KAG 返回时可能放行 LLM 伪造 `used_evidence` 的漏洞：无论白名单是否为空，候选引用都必须属于本轮实际 chunk ID 集合。
- 独立复跑：`python -m pytest tests/test_direct_qa.py -q`，29 passed；`python -m pytest tests/test_direct_qa.py tests/test_direct_qa_mastery.py tests/test_direct_qa_persistence.py -q`，77 passed；`git diff --check` 通过。
- 限制：服务尚未调用 persistence adapter，且当前通用 `EvidenceChunk` 合同没有已验证的 KG 节点关联字段；不能把 LLM 输出的任意非空字符串称为“有效 KG 节点”。

## 下一最小任务

只读架构澄清：核查实际 KAG chunk 的生产路径、metadata 与 KG 节点关联是否存在，并确定 Direct QA 验证 `confusion_kg_node_ids` 的最小真实来源；若需要新增数据结构并同时修改两个以上消费者，记录停止条件，不进入 Router 或前端开发。

### 阶段 4.1：有效 KG 节点真实来源澄清（通过，2026-07-14）

- `scripts/data_pipeline/kag_solver.py::hydrate_result()` 从生产 `chunks.jsonl` 的 `core_node_ids` 生成检索结果；`backend/services/retrieval/kag_singleton.py::_chunk_to_evidence_chunk()` 将其保留为 `EvidenceChunk.metadata["core_node_ids"]`。
- Codex 直接读取真实 `data/data/kb/kag_bge_m3/chunks.jsonl`，确认多个生产 chunk 均含真实 KG URI；Direct QA 目前仅在构造 LLM evidence 时丢弃了 metadata，LLM 任意非空 KG 字符串仍会通过规范化。
- 最小后续改动只涉及 `direct_qa.py`：收集本轮返回 chunk 的 `core_node_ids`，以其白名单过滤 `confusion_kg_node_ids`，不新增 schema、接口、缓存或第二条链路，未触发停止条件。
- 运行时风险：独立调用真实 `search_knowledge_base()` 因环境未安装 `faiss` 无法启动；这不是数据缺失，前端真实验收前必须解决或如实报告该 KAG 运行时依赖。
- 任务 `task-20260714-233005` 退出码 0，无产品或文档写入；Codex 独立复核通过。

## 下一最小任务

在 `direct_qa.py` 将本轮真实 KAG `EvidenceChunk.metadata["core_node_ids"]` 传入证据合同并对白名单外的 `confusion_kg_node_ids` 一律过滤；仅新增服务层测试，不调用 persistence adapter、Router 或前端。

### 阶段 4.2：本轮 KG 节点白名单验证（通过，2026-07-15）

- `direct_qa.py` 从每轮实际 `EvidenceChunk.metadata["core_node_ids"]` 收集去重集合，向受限 LLM evidence 合同暴露 `allowed_kg_node_ids`，并在返回前无条件取交集过滤 `confusion_kg_node_ids`。
- 已覆盖 metadata 允许/拒绝、空集合、多个 chunk 并集、混合伪造 ID、跨轮泄漏和畸形 metadata；`should_reinforce` 信号保留，但无有效节点时后续 adapter 不具备写入条件。
- 独立复跑：`python -m pytest tests/test_direct_qa.py -q`，37 passed；`python -m pytest tests/test_direct_qa.py tests/test_direct_qa_mastery.py tests/test_direct_qa_persistence.py -q`，85 passed；`git diff --check` 通过。
- 运行器 `task-20260714-235416` 记录退出码 1，但其退出不作为验收结论；Codex 已用完整范围检查和独立测试复核通过。未发现该任务对允许范围外文件的写入。

## 下一最小任务

让 Direct QA 成功结果在 `user_id` 存在时调用既有 `direct_qa_persistence` adapter；只在严格 `should_reinforce is True` 且本轮白名单后的有效 KG ID 非空时写入，持久化失败必须在 trace 如实标记且不得阻塞文字回答。不得创建 session、Router 或前端改动。

### 阶段 4.3：服务到画像持久化（通过，2026-07-15）

- Direct QA 成功结果仅在非空 user_id、严格 `should_reinforce is True`、且本轮白名单后 KG ID 非空时，经唯一 `direct_qa_persistence` adapter 更新 profile 与 Direct QA mastery；不直接写 SQLite、不创建课程 session。
- 持久化/导入异常不阻塞 answer，并在 `trace.persistence` 标记 failed；无资格条件时不创建 memory 或调用 adapter。
- 独立复跑：`python -m pytest tests/test_direct_qa.py tests/test_direct_qa_mastery.py tests/test_direct_qa_persistence.py -q`，93 passed；`git diff --check` 通过。
- 运行器 `task-20260715-000647` 退出码 1、无 completed marker；Codex 未以此判定验收，已独立复核允许范围与全部测试通过。

## 下一最小任务

在 MasterRouter 中以最小范围加入 `direct_qa` 意图与 dispatcher：无 session 的具体口腔问题调用 Direct QA 服务并形成 Router job `final_result.answer`；课程内 session 问答保持既有 interruption 路径。本轮不改前端。

### 阶段 5：Router Direct QA 分流（通过，2026-07-15）

- MasterRouter 新增 `direct_qa` 分类、目标/下一动作、history 规范化与 dispatcher；服务结果直接作为 `final_result` 返回，`final_result.answer` 为本轮 Direct QA answer。
- session 内既有 interruption 路径优先保留；Direct QA 不创建 session/PPT/outline。
- 独立复跑：`python -m pytest tests/test_master_router.py -q`，54 passed；`git diff --check` 通过。runner 非零退出不作为验收依据。

## 下一最小任务

核查并补足 Router API/job 到 master-chat 的 Direct QA 结果消费测试与最小兼容调整；之后由 Codex 进行真实页面验收。

### 阶段 5：Router job、聊天页与真实运行时验收（通过，2026-07-15）

- Router job 的 Direct QA 结果按原样放入 complete event 的 `router_result.final_result`；`learning_step` 为 `None`，不会调用课程编排、生成 PPT 或创建 session。独立复跑：`python -m pytest tests/test_router_jobs.py -q`，21 passed。
- 在真实 `master-chat` 页面提交本次具体问题“龋病的病因是什么？”，页面调度详情显示 `direct_qa`，并实际显示非空纯文字回答。页面 `master-chat.js` 的 complete-event 消费点将 `router_result.final_result.answer` 赋给当前消息内容；本次回答不是 placeholder。
- 在同一真实聊天会话继续提交“那细菌在其中起什么作用？”，页面显示第二个不同的 Direct QA 文字回答。该追问没有出现课程、PPT、大纲或文献列表；页面调度详情仍为 `direct_qa`。这同时是本次用户可见验收的唯一正向标记（第二轮实际回答）和反向标记（未显示旧缓存、学习/PPT/文献结果）。
- 真实 KAG 运行时已使用项目的 BGE-M3 本地模型、FAISS 索引和生产 `data/data/kb/kag_bge_m3` 数据完成检索；LLM 配置可用。前端服务健康检查返回 HTTP 200。
- 最终独立回归：`python -m pytest tests/test_direct_qa.py tests/test_direct_qa_mastery.py tests/test_direct_qa_persistence.py tests/test_master_router.py tests/test_router_jobs.py -q`，168 passed；`git diff --check` 通过。

## 最终结论

本计划的 Direct QA 范围已独立验证完成：无 session 的具体问题走 `direct_qa`、每轮 KAG 取证后输出文字回答、同一聊天会话可追问、课程 session 路径保留，且仅严格困惑信号与本轮有效 KG 节点会触发隔离的 Direct QA 画像/mastery 更新。未提交、推送或创建 PR；与 Direct QA 无关的既有 PPT 工作区改动未纳入本结论。
