# Direct QA 路由、追问记忆与学习画像联动计划

## 0. 文档目的与执行约束

本计划交付一个与课程学习并列的 **直接问答（`direct_qa`）** 路由。用户首次提出具体口腔医学问题时，系统直接从 KAG 检索并文字回答；用户可在同一聊天会话中连续追问。直接问答识别出的真实困惑还必须更新长期学习画像：标记薄弱知识点，并保守降低对应掌握度。

本文件是交付意图和验收顺序，不是实现完成的声明。实施时遵循：一个 durable runner、一次一个最小任务、每项完成后由 Codex 独立复核；禁止 commit、push、PR；不得修改 runner 基础设施、任务状态或本计划以外的无关模块。

## 1. 需求追溯记录

### Requirement: direct-qa-routing-memory-profile

- 用户来源：用户要求在原有“学习、计划、文献”分配路由之外加入首次直接提问；不做 PPT；可反复追问；并把检索到且判断为困惑的知识点反映到画像、掌握度和薄弱点。
- 用户意图/问题：一次性解决具体问题不应误开完整学习课程；连续追问要能理解上下文；真实困惑要驱动后续学习建议与薄弱点识别。
- 工作解释：新增 `direct_qa` 意图。它消费真实的用户消息、会话历史和 KAG 检索结果，返回纯文字答案。仅当 QA 语义判断为需强化且给出有效 `confusion_kg_node_ids` 时，才复用课程内 QA 的保守画像规则：写入薄弱点并对相应 KG 节点掌握度下调 `-0.03`。
- 明确非目标：不创建 learning session，不生成大纲/讲义/PPT，不把所有提问或所有搜索命中都视作薄弱，不将历史回答作为医学证据，不修改学习中 interruption QA 的行为，不新增跨设备的聊天记录产品。
- 范围与状态：无 `session_id` 的 master-chat 直接问答，以及同一聊天会话的后续追问；有 `session_id` 的请求继续进入既有学习 session policy。
- 不确定性与决策：用户已确认采用既有“仅困惑时扣分”规则。短期追问上下文复用当前 master-chat 实际提交的最近八条消息；它是会话上下文，不是长期医学记忆。Direct QA 的长期画像写入需要引入无课程来源的可追溯记录，不能伪造学习 session。

## 2. 当前实现事实与差距

| 当前事实 | 证据位置 | 对本计划的含义 |
| --- | --- | --- |
| MasterRouter 仅接受 `learning`、`study_plan`、`literature`、`clarification` | `backend/services/router/master_router.py` | 必须增加受服务端校验的 `direct_qa`。 |
| 无 session 时，路由分类后可启动 `start_lesson` | `MasterRouter.route()` / `_handle_learning()` | 具体问题的旧误路由必须被替换，不能创建课程。 |
| 课程内 QA 先用当前讲义/节点证据，再按需做 targeted KAG search | `backend/services/learning/qa.py`、`orchestrator.py` | Direct QA 可复用检索和证据规范化能力，但不能伪造 outline、handout 或 session。 |
| 课程内 QA 仅在 `should_reinforce=true` 且有 `confusion_kg_node_ids` 时对 mastery 做 `-0.03` | `LearningOrchestrator.handle_interruption()` | Direct QA 必须复用这一判断语义与幅度。 |
| `update_profile_from_qa()` 会区分困惑与普通扩展性问题 | `backend/services/learning/user_model.py` | 作为 Direct QA 画像更新的唯一语义函数，避免复制不同规则。 |
| `mastery_records.session_id` 为 `NOT NULL`，`upsert_mastery()` 按 user/session/KG 节点查找 | `backend/services/learning/memory.py` | 不能借用或伪造学习 session；需先完成最小的无课程来源 mastery 数据设计。 |
| 前端会保存并提交最近八条聊天消息 | `frontend/js/master-chat.js` 的 `historyPayload()` | 可作为 Direct QA 追问上下文的真实上游输入。 |

## 3. 行为分解

| 行为 | 必须可观察的结果 | 边界/失败情况 | 通用不变量 |
| --- | --- | --- | --- |
| 首次具体问题 | 路由为 `direct_qa`，执行 KAG 检索，返回文字答案 | 不创建学习 session、PPT 或大纲 | 由用户目标区分“具体求解”与“系统学习”，不是单个关键词。 |
| 系统学习请求 | 仍为 `learning` | 不因带问号或含“什么”而误分流 | 需要课程结构、连续讲解或完整主题学习时进入学习链路。 |
| 直接问答追问 | “那是什么机制/怎么处理”等短句可结合近期问答补全问题后重新检索 | 无足够上下文时请求澄清，不猜测 | 历史只用于解析指代与意图；每次回答证据仍来自本次 KAG。 |
| 普通扩展提问 | 可回答、可保留短期上下文 | 不降低 mastery，不增加薄弱点 | 提问本身不能等价于掌握不足。 |
| 困惑信号 | `should_reinforce=true` 且有有效 KG ID 时更新画像与 mastery | 没有 KG ID、LLM/检索失败或结果不可信时不扣分 | 只有已标识、可追溯的 KG 节点能触发持久化负向更新。 |
| 画像持久化 | `known_weak_points`、`mistake_patterns` 与对应 mastery 均可由用户画像读取 | 不污染某个真实课程 session 的事件、进度、统计 | Direct QA 记录须携带 `source=direct_qa`、用户、问题、证据和时间。 |
| 课程内提问 | 保持既有 interruption QA 和课程画像更新 | 不重新走总控 Direct QA | `session_id` 优先级高于新的首次问答分流。 |

## 4. 目标架构与责任边界

```text
master-chat 的真实 message/history/user_id
  -> MasterRouter（先处理 session context；否则分类）
  -> route.intent = direct_qa
  -> DirectQAService
       -> LLM：追问解析、检索规划、困惑判断、基于证据回答
       -> Python：KAG 调用、证据规范化、字段校验、持久化授权/事务
  -> Direct QA persistence adapter
       -> Direct QA 事件/来源记录
       -> update_profile_from_qa()
       -> source=direct_qa 的 mastery 更新（仅有效困惑 KG 节点）
  -> 统一 Router result
  -> master-chat 通用文字渲染
```

### 架构映射

| 行为 | 上游真源 | 所有者 | 中间契约 | 消费者 | 禁止旧路径 |
| --- | --- | --- | --- | --- | --- |
| 分类 | `message + history + context` | MasterRouter | 已校验的 route object | Router API / jobs | 无 session 的具体问题进入 `_handle_learning()`。 |
| 追问解析与检索 | 原问题、有限 history | DirectQAService | `resolved_question`、检索计划、KAG 结果 | 回答模型 | 用历史回答直接续写、不重新取证。 |
| 困惑判断 | 答案阶段的受限 JSON | DirectQAService | `should_reinforce`、`confusion_kg_node_ids` | persistence adapter | 将所有 `used_evidence` 或所有问题一律视作薄弱。 |
| 持久化 | 已验证的 QA 结果、user_id | LearningMemory 的 Direct QA 专用接口 | direct-QA event/source + mastery evidence | profile / plan / learning-profile read path | 创建虚假 learning session 或写入真实课程事件。 |
| 展示 | `final_result.answer` | master-chat | Router result | 聊天记录 | 学习/PPT/文献结果组件替代直接答案。 |

### LLM 与确定性代码边界

- LLM：判断意图中的直接问答语义、解析追问指代、提出检索查询、判断是否暴露困惑、在允许证据范围内生成文字回答。
- 确定性代码：服务端枚举校验、KAG 查询执行、证据 ID 标准化、限制持久化到有效 KG 节点、`-0.03` 掌握度变更、事务/授权、测试。
- 禁止：用关键词规则替代困惑判断；用规则答案替代 KAG+LLM 正式回答；在模型故障时自动启动学习。

## 5. 数据与持久化设计前提

Direct QA 的画像更新不能直接调用当前要求 `session_id` 的课程 mastery 写入接口，也不能创建“假课程”。因此第一项开发工作必须冻结一个最小、可迁移的来源模型；只有通过该架构关卡才能继续。

推荐的最小目标是让 mastery 与事件记录拥有显式来源：

- `source`：`learning` 或 `direct_qa`；
- `source_ref`：真实课程 session ID（learning）或真实 Direct QA conversation/event ID（direct_qa）；
- `session_id`：仅学习来源使用，Direct QA 可为空；
- 每条 Direct QA 更新必须保存 `question`、`resolved_question`、`confusion_kg_node_ids`、实际 `used_evidence`、时间及 `source=direct_qa`。

实施不应在本计划中预先假定具体 SQLite migration 形式。阶段 1 必须核对所有 `mastery_records`、事件查询和画像汇总消费者；若需要同时改数据结构和两个以上 API/缓存/编排/渲染消费者，停止并进行独立架构澄清，而不是把它伪装成普通修补。

## 6. 开发顺序

### 阶段 1：只读契约冻结与最小迁移设计

**目标**：以代码、数据库 schema 和调用者为证据，冻结 Direct QA 事件与无课程 mastery 的最小契约。

**核查内容**：

1. 枚举 `mastery_records`、`lesson_events`、`learning_summaries`、`user_profiles` 和画像读取/汇总的所有生产调用者。
2. 确认哪些消费者依赖 `session_id NOT NULL`，以及 profile 页实际如何消费 `mastery_by_kg_node`、`known_weak_points`。
3. 明确 Direct QA event 是否需要独立表或对现有事件表进行受控 schema 扩展；禁止借用课程 session。
4. 明确 migration 兼容旧 SQLite 的路径与回退语义。

**可达到证据状态**：`designed`。
**停止条件**：发现需更换整条画像链、同时变更数据结构和两个以上消费者、或需新建并行画像系统时，停止并提交架构澄清，不进入阶段 2。

### 阶段 2：Direct QA 持久化契约与画像更新基础

**目标**：实现经过阶段 1 批准的最小 schema/API，使 Direct QA 可持久化自己的来源记录并安全更新已有画像。

**范围**：

1. 添加 Direct QA 专用的事件/来源写入与读取接口；记录必须归属 user_id，且不创建 learning session。
2. 让 mastery 写入能表达 `source=direct_qa`，以 KG 节点为准查找/更新，保留历史证据。
3. 复用 `update_profile_from_qa()`，且只在有效困惑信号时调用 mastery 下调。
4. 对同一用户、同一 KG 节点的重复 Direct QA 困惑更新必须可预测、可审计并受 `[0, 1]` 边界约束。

**非目标**：不修改课程内 QA 的既有写入结果；不做前端展示样式；不因普通提问更新画像。
**可达到证据状态**：`implemented`，经独立数据库/API 测试后可达 `verified`。

### 阶段 3：Direct QA 服务（追问、KAG、困惑输出）

**目标**：建立独立服务，例如 `backend/services/qa/direct_qa.py`，输入 `message + history + user_id`，输出可验证的 Direct QA 契约。

**流程**：

1. 使用最近的真实聊天 history 解析追问；若无法消歧，返回需要澄清的 Direct QA 结果。
2. LLM 生成有限检索计划，Python 复用现有 KAG 检索和结果规范化能力。
3. LLM 基于问题和本次 KAG 证据生成回答，同时输出 `should_reinforce` 与 `confusion_kg_node_ids`。
4. 服务端验证：混淆 KG ID 必须来自本次允许的证据/检索关联范围；无有效 ID 时不允许 mastery 下调。
5. 回答成功后调用阶段 2 的 persistence adapter；持久化失败不得伪装为已更新画像，需在 trace 中标示并保持回答可用。

**非目标**：不传入课程 handout/outline；不调用 `handle_interruption()`；不使用历史回答作为引用来源。
**可达到证据状态**：`implemented`，以生产形态 KAG 链路和持久化证据验证后可达 `verified`。

### 阶段 4：总控、API 与 job 接入

**目标**：把 Direct QA 变成受控且统一的总控意图。

**范围**：

1. 在 `VALID_INTENTS`、目标映射、下一动作、分类 prompt、route 校验和 dispatcher 中加入 `direct_qa`。
2. 分类器需基于 history 将指代性追问延续为 `direct_qa`；有 `session_id` 时保持原 session policy 优先。
3. 增加 `_handle_direct_qa()`，仅调用 DirectQAService，返回 `next_action=show_direct_answer`。
4. 使 `/api/router/chat` 与 `/api/router/jobs` 均保留相同的 Direct QA result contract，并添加相应进度状态文案。

**禁止**：从 `direct_qa` 调用 `start_lesson`、生成 PPT、创建大纲，或在 router 层直接拼接答案/写画像。
**可达到证据状态**：`implemented`，API 与 job 双路径测试后可达 `verified`。

### 阶段 5：聊天页真实链路验证与最小兼容调整

**目标**：确认现有 `resultAnswer()` 能消费 `final_result.answer`；仅在实际不能消费时添加最小的非视觉分支。

**范围**：

1. 验证 `historyPayload()` 发送的真实历史能被 Direct QA 服务消费，且答案被保存到当前聊天会话以便下一轮追问。
2. 从真实 browser chat → router job → Direct QA result → DOM 验证文本结果。
3. 不新增卡片、颜色、动画或新交互；如未来需新增可见表现，先完成用户体验澄清。

**可达到证据状态**：`verified`；用户查看真实结果并认可后才为 `human-accepted`。

### 阶段 6：全链路回归、健康检查与交付验收

**目标**：独立验证新路由不改变既有学习、计划、文献和课程内 QA 的责任边界。

**范围**：运行后端单测、迁移/持久化测试、Router API/job 测试、课程 session 回归、聊天页真实运行时验证；完成后执行一次只读项目健康检查。

## 7. 验证矩阵

| 场景 | 正向证据 | 反向证据 |
| --- | --- | --- |
| 首次直接问题：“牙髓炎与根尖周炎如何鉴别？” | route 为 `direct_qa`；存在本次 KAG evidence；答案显示 | 无 session、outline、PPT 资源；未调用 `start_lesson`。 |
| 追问：“那疼痛特点呢？” | `conversation_context_used=true`；解析后的问题与首问语境一致；重新检索 | 不把上一轮 answer 当成唯一证据；无上下文时不臆测。 |
| 系统学习：“系统学习牙周炎” | route 为 `learning`，产生真实学习 session | 不进入 Direct QA persistence。 |
| 课程内提问 | route source 为 session policy / interruption QA | 不被 Direct QA 分类接管。 |
| 普通扩展问题 | 直接回答且 `should_reinforce=false` | profile/memory/mastery 无负向变更。 |
| 困惑问题 | 只对有效 `confusion_kg_node_ids` 写入弱点，mastery 每节点 `-0.03`，带 source/evidence | 不对所有检索 chunk/所有 related KG 节点扣分；不写假 session。 |
| KAG 无命中或 LLM 故障 | 保守提示，trace 反映失败状态 | 不虚构引用，不自动开始课程，不写负向画像。 |
| 用户隔离 | user A 的 Direct QA 不改变 user B 的 profile/mastery | 无跨用户读取、写入或 history 泄漏。 |

所有测试必须至少包括两个结构不同的口腔主题，禁止使用主题名、KG ID、任务 ID、数组下标或固定答案作为实现分流条件。

## 8. 最终验收标准

只有以下全部由 Codex 独立验证，才能将本计划标记为 `verified`：

1. **前端真实可回复（最终交付门槛）**：从实际 `master-chat` 页面输入一个具体口腔问题，用户可提交请求、看到非错误的完成态，并在同一对话气泡中看到本次 Direct QA 生成的非空文字回答。必须证明页面实际消费的是本次 Router job 的 `final_result.answer`，而非 placeholder、旧 localStorage 缓存、手写 DOM 文本或课程/PPT 内容；若 KAG 或回答模型的必要配置不可用，系统应显示真实的保守不可用说明，该状态不能被表述为“可正常回复”。
2. **正确分流**：在无 session 的真实 API 与浏览器入口中，具体问题为 `direct_qa`，系统学习、计划、文献和澄清请求仍保持原路由；无关键词硬编码。
3. **真实证据链**：真实 `message/history/user_id` 经 DirectQAService 产生检索计划，调用 KAG，KAG 结果被答案生成与 `used_evidence` 消费；不可用时有如实失败/不足提示。
4. **追问可用**：同一聊天会话的一次指代性追问可引用最近上下文来补全检索问题，并完成新的 KAG 检索；脱离上下文的等价短句不会被猜测性回答。
5. **画像语义正确**：普通扩展提问不会降低 mastery 或新增薄弱点；仅 `should_reinforce=true` 且有效 `confusion_kg_node_ids` 时，调用既有 `update_profile_from_qa()` 语义并按 `-0.03` 降低相应节点。
6. **持久化无污染**：每个 Direct QA 负向更新都能追溯到 user、时间、问题、KAG 证据和 `source=direct_qa`；不创建或伪造 learning session，不改变课程进度、课程事件或课程统计。
7. **用户隔离与数据边界**：不同 user 的 Direct QA 画像、mastery 和记录严格隔离；历史回答不作为医学证据。
8. **用户可见结果证据**：验收要记录一个来自本次目标结果的唯一正向标记，及一个证明未显示旧缓存/学习结果/PPT/文献列表的唯一反向标记。
9. **回归无破坏**：既有 master-router、学习 session/interruption QA、学习画像和聊天页测试通过；无关工作区变更不被纳入本项目。
10. **最终健康检查**：只读检查 git 状态、最近提交、变更范围、生产形态链路、已运行命令和 `cannot prove` 项；任何缺少独立证据的项目不得标记通过。

## 9. 需求—证据账本

| Requirement | 行为解释 | 计划所有者 | 当前状态 | 需要的证据 | 禁止结果 | 当前结论 |
| --- | --- | --- | --- | --- | --- | --- |
| 首次直接问答 | 无 session 时具体问题从 KAG 直接回答 | MasterRouter + DirectQAService | designed | route、检索、答案的真实链路 | 启动课程/PPT | cannot-prove |
| 连续追问 | history 只用于语义补全，每轮重新取证 | DirectQAService | designed | 指代追问与无上下文对照 | 用旧回答替代检索 | cannot-prove |
| 困惑驱动画像 | 仅有效困惑 KG 节点降 mastery、写弱点 | persistence adapter + user model | designed | 画像前后、mastery 前后、来源记录 | 所有问题/搜索命中一律扣分 | cannot-prove |
| 数据来源隔离 | Direct QA 不伪造课程 session | LearningMemory migration | understood | schema、事件、消费者链路 | 污染课程进度/统计 | cannot-prove |
| 既有链路不回归 | session 内 QA 和原三类路由不变 | Router/Orchestrator regression | designed | 回归测试和运行时请求 | Direct QA 截走课程问题 | cannot-prove |

## 10. 计划修订记录

- 2026-07-14：初始计划。用户要求增加首次直接问答路由，KAG 检索后直接文字回答，不生成 PPT。
- 2026-07-14：加入会话级追问记忆。原因：用户要求问完后可反复追问；复用 master-chat 实际 history 输入，禁止将历史回答充当医学依据。
- 2026-07-14：加入画像与 mastery 联动。原因：用户要求检索到且体现困惑的知识点降低掌握度并形成薄弱点；用户确认复用现有课程 QA 的保守规则，而非所有提问均扣分。
- 2026-07-14：加强最终用户可见验收。原因：用户明确要求在前端提问后能正常获得回复；因此真实聊天页的非空 Direct QA 回答成为最终交付硬门槛，接口/单测通过不能替代。
- 2026-07-14：阶段 1 架构冻结。原因：SQLite 不能就地放宽 `mastery_records.session_id NOT NULL`；重建课程 mastery 表会触发多消费者链路扩张。改为新增物理隔离的 `direct_qa_mastery_records` 与专用接口，课程 session 表和既有消费者不改。
