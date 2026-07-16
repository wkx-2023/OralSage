# 对话式学习计划闭环 V1 进度

## 当前状态

- 计划文档：`docs/CHAT_TO_STUDY_PLAN_CLOSED_LOOP_V1_zh.md`
- 分支：`codex/direct-qa-routing-memory`
- Git 纪律：不暂存、不提交、不推送。
- 已验证完成的开发任务：`task-20260715-084946`（PlanDraft 内存存储契约）、`task-20260715-085821`（父子草案关联）、`task-20260715-090659` 与返工 `task-20260715-091333`（章节记录到严格 `learning_scope` 的事实解析）、`task-20260715-092146`（结构化计划请求规范化）、`task-20260715-093719`（结构化请求到临时草案）、`task-20260715-095208` 与返工 `task-20260715-100501`（自然语言计划请求的受约束语义解析）、`task-20260715-101941`（自然语言到临时草案组合服务）。Codex 独立复核：计划相关任务仅修改允许的 `study_plan.py` 与 `test_study_plan_memory.py`；最新复跑 `python -m pytest tests/test_study_plan_memory.py -q` 为 `172 passed`；`git diff --check` 通过；无 commit/push/PR；`docs/project-notes.md` 未改动。
- 已验证完成的开发任务补充：`task-20260715-102804` 与返工 `task-20260715-104426`（现有推荐服务的可选对话草案分支及明确异常交接）。Codex 独立复核：定制分支用 KG chapter provider → 受约束解析 → PlanDraftStore → 推荐卡片；无文本仍走原 deterministic 分支且不调用 KG/LLM；父草案不存在/越权/过期及 scope 无效错误保留其 `KeyError`、`PermissionError`、`ValueError` 类型，不回退为默认推荐。`python -m pytest tests/test_study_plan_memory.py -q` 为 `190 passed`；`git diff --check` 通过；无 commit/push/PR；`docs/project-notes.md` 未创建或改动。
- 已验证完成的开发任务补充：`task-20260715-132135`（推荐 HTTP 请求接线）。Codex 独立复核：`LearningPlanRecommendRequest` 新增可选 `request_text`，`POST /api/learning/plan/recommend` 原样传给既有 `recommend_study_plan`；未提供该字段的旧调用仍使用默认值 `None`。复跑 `python -m pytest tests/test_master_router.py tests/test_router_jobs.py tests/test_study_plan_memory.py -q` 为 `267 passed`；完整 diff 仅含允许的 `backend/main.py`、`backend/schemas.py` 和相关既有任务文件；`git diff --check` 通过；无 commit/push/PR；`docs/project-notes.md` 未创建或改动。
- 已验证完成的开发任务补充：`task-20260715-133912`（草案读取 HTTP 接口）。Codex 独立复核：`GET /api/learning/plan/drafts/{plan_draft_id}?user_id=...` 只调用既有 `get_draft_from_default_store`；同一用户成功读取真实默认草案，未知、越权、过期分别得到 404、403、410，响应不含草案 payload 且不回退为默认推荐。Runner 非零退出由 SessionEnd hook cancelled 引起，未作为验收证据。复跑 `python -m pytest tests/test_learning_plan_api.py tests/test_study_plan_memory.py -q` 为 `195 passed`；`python -m pytest tests/test_master_router.py tests/test_router_jobs.py -q` 为 `77 passed`；`git diff --check` 通过；无 commit/push/PR；`docs/project-notes.md` 未创建或改动。
- 已验证完成的开发任务补充：`task-20260715-135430` 与证据返工 `task-20260715-140630`、工件纠正 `task-20260715-142312`（聊天草案卡片链接）。Codex 独立复核：真实 `master-chat.html` 加载 `master-chat.js` 后，带草案的卡片显示“打开学习计划”且不显示“开始学习”；无草案的旧推荐仍显示开始学习。正反运行时截图已独立查看并转存系统临时目录；工作区证据工件已精确清理，未碰其他未提交文件。Node 契约测试为 `30 passed`，语法检查与 `git diff --check` 通过；无 commit/push/PR；`docs/project-notes.md` 未创建或改动。
- 已验证完成的开发任务补充：`task-20260715-143422`、真实契约返工 `task-20260715-144813` 与运行时证据返工 `task-20260715-151855`（计划页草案读取）。Codex 独立复核：计划页消费真实 GET 返回的 `result.payload.items`；有效草案显示“聊天计划草案”和 5 张卡片，未知草案显示 404 错误、0 卡片且不回退默认推荐。无遮盖正反截图已独立查看。`node tests/test_learning_plan_draft_load.js` 为 `49 passed`、`python -m pytest tests/test_learning_plan_api.py -q` 为 `5 passed`；无 commit/push/PR；`docs/project-notes.md` 未创建或改动。
- 已验证完成的开发任务补充：`task-20260715-153618`（正式计划项的 `learning_scope` 持久化）。Codex 独立复核：SQLite migration、旧行读取兼容、创建与更新接口均保留/往返可选 scope；未传 scope 的旧计划项保持空范围。复跑 `python -m pytest tests/test_study_plan_memory.py tests/test_learning_plan_api.py -q` 为 `212 passed`；`git diff --check` 通过；无 commit/push/PR；`docs/project-notes.md` 未创建或改动。
- 已验证完成的开发任务补充：`task-20260715-155547`（聊天草案采纳 payload）。Codex 独立复核：计划页将草案的 `learning_scope`、`task_type`、`learning_target` 与依据随既有创建请求提交；旧系统推荐没有 scope 时仍维持既有 payload。复跑 `node tests/test_learning_plan_draft_load.js` 为 `60 passed`；`git diff --check` 通过；无 commit/push/PR；`docs/project-notes.md` 未创建或改动。
- 已验证完成的开发任务补充：`task-20260715-161013`（正式计划项 scope 到学习编排的 handoff）。Codex 独立复核：Router 只对非空、正式计划项的 `learning_scope` 调用 `start_lesson`；普通直接学习与空 scope 不传该可选参数；orchestrator 将同一范围传给 planner 并记录在 lesson-started event/返回值。复跑 `python -m pytest tests/test_master_router.py tests/test_router_jobs.py -q` 为 `83 passed`；无 commit/push/PR；`docs/project-notes.md` 未创建或改动。
- 已验证完成的开发任务补充：`task-20260715-163245` 与超时返工 `task-20260715-173641`（planner 范围过滤与确定性测试）。Codex 独立复核：`plan_lesson` 按 `document_id`、章节与 `kg_node_ids` 过滤证据/KG 节点；任一指定范围无可用资源时返回 `scope_no_resources`，不回退全局规划。返工将会继续进入 planner 的单测显式替换为本地 fallback，避免真实 LLM 调用造成超时；这只修复测试证据，不改变生产 LLM 路径。复跑 `python -m pytest tests/test_master_router.py::TestPlanLessonScopeFiltering -q` 为 `12 passed in 0.44s`，`python -m pytest tests/test_master_router.py tests/test_router_jobs.py -q` 为 `97 passed in 2.30s`；`git diff --check` 通过；无 commit/push/PR；`docs/project-notes.md` 未创建或改动。
- 已验证完成的开发任务补充：`task-20260715-175003`（继续课程计划项恢复当前节点）。Codex 独立复核：正式 `continue_lesson` 计划项从 Router 调用既有 `next_step(linked_course_id)` 并返回当前 Tutor step；不调用 `continue_to_next_section`，末节点也不由启动动作制造 `lesson_completed=True`。显式 `context.session_id` 的普通继续保持原推进语义；此分支不调用计划项完成、更新或延期。复跑 `python -m pytest tests/test_master_router.py tests/test_router_jobs.py -q` 为 `101 passed in 3.04s`；`git diff --check` 通过；无 commit/push/PR；`docs/project-notes.md` 未创建或改动。
- 已验证完成的开发任务补充：返工 `task-20260715-184211`（聊天草案 lineage）。Codex 独立复核：每个聊天会话持有独立 `currentPlanDraftId`，只从成功 `study_plan` 完成结果取得；后续消息将其送入 context，Router 仅在最终 `study_plan` 分支向既有推荐服务传 `parent_plan_draft_id`。错误、非计划 intent、无草案结果和会话切换不会伪造或泄漏该 ID，且未引入正式计划写入。复跑 `node tests/test_master_chat_plan_draft.js` 为 `14 passed`，`python -m pytest tests/test_master_router.py tests/test_router_jobs.py -q` 为 `112 passed in 4.51s`；`git diff --check` 通过；无 commit/push/PR；`docs/project-notes.md` 未创建或改动。
- 已验证完成的开发任务补充：`task-20260715-190027`（正式计划项 scope 闭环合同）。Codex 独立复核：正式计划项的 scope 经既有创建/读取路径保持，Router 只将其交给学习启动；无 scope 和直接学习保持兼容，课程完成不会替用户完成计划项。复跑 `python -m pytest tests/test_learning_plan_api.py tests/test_master_router.py tests/test_router_jobs.py -q` 为 `122 passed in 6.33s`；`git diff --check` 通过；无 commit/push/PR；`docs/project-notes.md` 未创建或改动。
- 已验证完成的开发任务补充：`task-20260715-191438`（草案异常与旧调用兼容）。Codex 独立复核：草案异常与无文本旧推荐契约均由本地测试覆盖；复跑 `python -m pytest tests/test_learning_plan_api.py tests/test_study_plan_memory.py tests/test_master_router.py -q` 为 `305 passed in 11.63s`；`git diff --check` 通过；无 commit/push/PR。
- 当前 runner 任务：无；下一任务为把聊天中最近生成的草案 ID 作为 `parent_plan_draft_id` 仅在后续 `study_plan` 请求时传到既有草案服务，以兑现采纳前后“继续说想法 → 新草案、不自动改日历”的 lineage 契约。

## 需求追溯账本

| Requirement | Interpretation/behavior | Planned owner | Implementation status | Evidence required | Prohibited legacy result | Current verdict |
| --- | --- | --- | --- | --- | --- | --- |
| 对话定制计划草案 | `study_plan` 必须保留本次用户计划所对应的结构化草案，供现有计划页采纳 | `backend.services.learning.study_plan` | implemented | 用户隔离、TTL、不可变读回、受约束自然语言解析、KG→草案→推荐卡片、章节事实范围与失败不退回默认推荐测试；后续真实聊天→计划页链路 | 重新计算默认推荐冒充用户刚生成的计划 | verified（推荐服务内部；尚未接聊天/API） |
| 对话持续调整 | 用户可在采纳前后对话提出修改；每次只创建新草案，绝不自动覆盖已采纳计划 | `study_plan` 草案生成与聊天上下文 | implemented | 父草案/新草案 ID 独立、正式计划项未变的测试 | 用聊天重生成静默修改用户日历 | verified（仅草案 lineage 与结构化再生成） |
| 计划页采纳 | 现有计划页加载聊天草案并将其保存为正式日历计划项 | 计划页与学习计划 API | understood | 草案 ID、采纳 payload、数据库 reload | 计划页重新显示不相关的固定推荐 | designed |
| 计划项精确学习 | 已采纳项按真实教材/章节/KG 范围启动学习 | Router 与学习规划器 | understood | 正式计划项 scope → planner trace/outline 的运行时证据 | 只用标题模糊搜索或直接完成 | designed |
| 状态分离 | session 完成不得自动完成计划项；续学先恢复当前节点 | Router 与学习编排器 | understood | session/plan 状态的独立测试 | 启动 continue_lesson 即推进并显示完成 | designed |

## 已知约束与风险

- 工作区存在用户既有未提交修改；后续审查必须逐项区分，禁止覆盖或纳入任务范围。
- 本期保留现有意图分类与调度；计划服务消费已被路由的原始用户消息。
- `task-20260715-084946` runner 的退出码为 3，但 Codex 未采信 runner 报告：独立复跑正式测试通过并核查完整允许范围 diff，故草案存储契约判定为通过；runner 退出码异常保留为调度诊断，不作为产品失败结论。
- `task-20260715-085821` runner 的退出码为 3 且标记为失败，但 Codex 独立复跑正式测试通过并核查完整允许范围 diff，故父子草案契约判定为通过；该 runner 状态异常保留为调度诊断，不作为产品失败结论。
- 流程纠正（2026-07-15）：用户更新 `unattended-development` skill 后指出此前没有遵循新版发布闭环。经 Codex 重读，已确认 `task-20260715-102804` 在投递前只有摘要性说明，缺少新版 `task-contract.md` 的完整用户可见 briefing/标准回执，属于发布流程违规，不是产品任务通过证据。该任务保持运行并在终态按产品质量独立验收；本条记录为当前流程风险，后续每个任务必须先完整 briefing、再提交、在 PID 存活后发送标准回执。
- 只读健康核验（`task-20260715-110055`，Codex 独立复核）：已确认聊天输入可到达 `MasterRouter._handle_study_plan`，但该函数没有把 `message` 透传为 `recommend_study_plan(request_text=...)`，所以定制草案分支在真实 Router 路径不可达。聊天卡片不携带 `plan_draft_id`，计划页不读取它，推荐 API 也不透传 `request_text`；这些均为后续任务，当前标记 `cannot-prove`，不能宣称闭环。旧 `recommendation_id` 重算默认推荐路径仍存在。该核验只读，未改产品文件、未运行会写状态的测试；`docs/project-notes.md` 未创建或改动。
- `task-20260715-110818`（Codex 独立复核）：`MasterRouter._handle_study_plan` 现将原始 `message` 透传为 `request_text`；两条结构不同的 study_plan 文本及非 study_plan 负向路径均有 Router 测试。复跑 `tests/test_master_router.py tests/test_router_jobs.py` 与 `tests/test_study_plan_memory.py` 通过；Router 分类、其他 dispatch、API/前端/DB 未改。该链接已验证，但 HTTP 与页面草案消费仍为 cannot-prove。
