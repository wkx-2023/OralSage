# 对话式学习计划 V1：无人值守开发计划

## 需求记录与变更记录

### Change request: chat-study-plan-v1-follow-up

- Trigger/source: 用户要求使用 `docs/CHAT_STUDY_PLAN_V1_HANDOFF_zh.md` 完成本轮开发。
- Desired outcome: 对话计划按单章、整本书和无指定内容三种模式生成可执行且有真实范围/依据的学习、复习、练习任务，并将其正确接入既有执行链路。
- Current observed problem or limitation: `study_plan._build_draft_items` 仍以相同范围和理由的“（1/N）”机械拆分任务；交接文档列出的内容结构和练习执行语义尚未完成。
- Scope: 后端计划生成、正式计划项契约、计划项启动路由、既有阶段卷接线及回归/运行时证据。
- Preserve: MasterRouter 分类及其他 intent；普通直接学习；显式完成计划项；既有阶段卷出题、判分、掌握度回写；用户已有未提交文件。
- Replace/remove: 计划草案中的机械编号拆分；将 `practice` 静默当作普通定向学习的执行路径。
- Compatibility/migration: 旧正式计划项和旧无文本推荐必须可读可用；新字段需向后兼容。
- Acceptance evidence: 覆盖三种输入模式和无数据追问的后端测试；范围/链路映射测试；以真实 KG 数据独立验证单章与整书草案。
- Unknowns requiring user decision: 无。交接文档已定义产品语义和明确非目标。

### Requirement: structured-chat-study-plan-v1

- User source: `CHAT_STUDY_PLAN_V1_HANDOFF_zh.md` 第 3、4 节及用户本轮指令。
- User intent/problem: 让计划任务真实反映教材、章节与知识点范围，并把学习、复习、基础练习和提高练习送至各自已有能力，而非生成重复或误导性的任务。
- Working interpretation: 先以真实章节/KG 数据构建可展示的范围，再按三种输入模式生成有日期、语义和依据的草案；其后扩展正式任务契约并接通执行路由。
- Explicit non-goals / forbidden interpretations: 不改前端；不新建复习引擎；不承诺“高难题”；不改 MasterRouter 分类、题库、判卷或掌握度回写。
- Scope and affected users/data/states: `study_plan` 草案、正式计划项、计划项启动及现有 session/阶段卷边界。
- Uncertainty and decision boundary: 无材料不确定性；范围、任务语义与证据要求由交接文档明定，运行时事实以代码和测试为准。
- Change record: 本文上述 `chat-study-plan-v1-follow-up`。

### Change request: explicit-review-plan-semantics

- Trigger/source: 用户补充确认：学习和复习生成的计划不同；“复习某书某章”必须有独立实现。
- Desired outcome: 用户明确请求复习教材/章节时，系统生成和执行真正的复习计划，而不是把学习计划改标题或将练习静默当普通学习。
- Current observed problem or limitation: 现有草案 intent 只区分 `learn_chapter` / `review_chapter` 的标签与理由，机械多天任务没有复习结构、练习层级或依赖语义。
- Scope: R2 计划生成、R3 正式任务契约、R4 执行映射及其测试；不改变当前 R1 名称范围任务。
- Preserve: `learning_scope` 的真实范围；普通直接学习；既有定向学习、阶段卷生成/提交/判分/掌握度回写；显式完成正式计划项。
- Replace/remove: 将“复习”仅作为学习任务标题/理由的路径替换为独立 task type、依赖和执行语义；禁止 `practice` 默认路由到普通学习。
- Compatibility/migration: 旧 `learn_chapter` / `review_chapter` 项必须可读；新字段和 task type 以可选方式扩展，历史项继续走其既有兼容路径。
- Acceptance evidence: 生成测试证明学习/复习计划的 task type、顺序、依据和依赖不同；路由测试证明复习使用 scoped targeted learning、基础/提高练习各自使用正确既有阶段卷链路；无弱点证据时不得虚称提高练习。
- Unknowns requiring user decision: 无。用户已确认需要独立复习实现；交接文档已定义复习与提高练习的既有能力边界。

### Requirement: explicit-review-plan-semantics

- User source: 用户本轮确认“学习和复习生成的计划是不同的”，并要求修改计划。
- User intent/problem: 用户输入“复习《某书》某章”时，得到以复习为目的的任务结构和执行，而非重新学习的伪装。
- Working interpretation: `review_chapter` 是独立生成模式：默认生成“复习本章 → 提高练习”；复习复用同范围的既有定向学习，且提高练习只在基础练习结果或 KG 掌握度提供真实弱点范围时建立定向 session 后复用阶段卷。无弱点证据时必须诚实地降级/说明，不能承诺提高或难题。
- Explicit non-goals / forbidden interpretations: 不新造复习引擎；不改阶段卷出题、判卷、掌握度回写或难度筛选；不把更多题目声称为高难题；不改前端。
- Scope and affected users/data/states: 对话草案及正式计划项的 task type、task dependency、practice level、学习 scope 和关联 session；计划项点击后的 Router/orchestrator 调度。
- Uncertainty and decision boundary: 无；复习定义、提高练习的证据边界和不承诺难题已由交接文档第 3.2 明定。
- Change record: 本文上述 `explicit-review-plan-semantics`。

### Change request: llm-personalized-plan-orchestration

- Trigger/source: 用户明确要求固定“第一天学习、第二天复习”等章节模板不得替代 LLM；主路径必须由 LLM 生成个性化计划。
- Desired outcome: 已有明确范围或足够真实学习信号时，计划的任务顺序、比例、重点、节奏和理由能反映用户请求、时间预算、画像和真实学习/掌握度证据，而不是套用统一日程。
- Current observed problem or limitation: 当前生成器依据 days/daily_minutes 用固定循环机械展开项目；即使已有 LLM 文本解析，也没有受约束的 LLM 编排阶段。
- Scope: R2/R6 的生成架构、提示/JSON contract、服务端校验、失败/澄清处理与测试；R3/R4 继续消费经过校验的统一任务契约。
- Preserve: KG/资料范围、学习记录、阶段卷结果与掌握度是事实真源；确定性代码仍负责 schema、预算、日期、依赖、权限和执行映射；无数据时自然追问；既有无文本推荐兼容。
- Replace/remove: 删除“固定章节学习→复习→练习序列可替代 LLM 输出”的路径。任何非空计划请求必须调用 LLM；规则代码不得在 LLM 不可用、超时、输出不合约或候选不足时生成模板计划，而应返回可读失败/澄清结果并保留可重试上下文。
- Compatibility/migration: 草案项继续使用向后兼容的统一 contract；旧草案/正式项可读。新增 LLM 编排输出只在服务端校验通过后写入，不迁移历史项。
- Acceptance evidence: 使用结构不同的受控输入证明每个非空计划请求实际调用 LLM 且可改变任务顺序、比例、重点和理由；验证它绝不能越出候选范围、预算或证据；验证 LLM 失败/非法输出返回明确失败/澄清而不生成硬编码计划。
- Unknowns requiring user decision: 无。用户已明确授权 LLM 生成个性化计划，事实和执行边界仍由交接文档确定。

### Requirement: llm-personalized-plan-orchestration

- User source: 用户本轮确认“输入计划以后肯定调用 LLM，而不是代码生成”。
- User intent/problem: 同样的教材章节和天数不应总产出相同计划；用户应获得适配重点、节奏、时间和学习证据的方案。
- Working interpretation: 对任何非空计划请求，服务必须先构造已验证的上下文并调用 LLM。LLM 输出 schema 约束的编排候选（task type、scope reference、date offset、minutes、reason、dependency）；服务端验证并补全真实 scope 后才持久化。候选不足时 LLM 负责生成自然澄清；LLM 不可用或输出不合约时返回明确可重试错误/澄清，绝不以固定日程替代。
- Explicit non-goals / forbidden interpretations: LLM 不得虚构教材、章节、知识点、薄弱点、阶段卷结果或难度；不得绕过 scope 校验、预算、显式依赖和既有执行路由；不得直接写正式计划项；确定性代码不得生成任何计划任务、日期、顺序或理由来冒充 LLM 计划。
- Scope and affected users/data/states: 自然语言计划请求的草案生成；LLM 编排 prompt/response contract；草案理由和日程；服务端 validation、失败/澄清处理；后续正式任务消费。
- Uncertainty and decision boundary: 无；用户已授权个性化编排，既有交接文档指定事实真源与禁止虚构的边界。
- Change record: 本文上述 `llm-personalized-plan-orchestration`。

### Change request: clarification-response-runtime-handoff

- Trigger/source: 用户在已重启的真实聊天页提交“给我制定一个学习计划”后，只看见“已生成学习计划建议”和空“调度详情”；用户明确指出“不改前端”不允许导致用户收不到回复。
- Desired outcome: 当范围不足而 LLM 编排返回澄清时，聊天页必须显示可读、可操作的澄清问题，不能伪称已生成计划或渲染空调度详情。
- Current observed problem or limitation: `create_plan_draft_from_text` 持久化 `clarification_needed=true` 和空 items，但 `_recommend_custom_draft` 未把该状态/澄清文字暴露给聊天结果；`master-chat.js` 将任何空 recommendations 当成功建议，降级为默认成功文案。
- Scope: 仅计划草案澄清结果的 LLM contract → 后端结果 → 现有聊天渲染 handoff，以及其测试和真实页面验收。
- Preserve: 有 items 的正常个性化计划卡片；非空计划必须两次调用 LLM；LLM 不得由硬编码计划替代；不改其他前端页面、阶段卷、MasterRouter 分类、普通学习和正式计划项。
- Replace/remove: 聊天页对空 recommendations 的“已生成学习计划建议”默认文案及空调度详情；其只在正式计划澄清状态中禁用。
- Compatibility/migration: 旧草案或旧后端结果未携带澄清字段时维持现有非澄清渲染；无需迁移持久化正式计划项。
- Acceptance evidence: 两个结构不同的无范围请求经真实路由/服务返回可读澄清并在聊天页显示；指定章节请求仍显示计划卡片；浏览器运行时证明正向澄清标记出现、旧默认成功标记/空调度详情不出现。
- Unknowns requiring user decision: 无。用户已明确要求不能因前端限制而无回复；现有聊天页面的澄清响应样式可复用，不涉及新的审美或交互选择。

### Requirement: clarification-response-runtime-handoff

- User source: 用户反馈“你让我收不到任何回复那不是搞笑吗？”。
- User intent/problem: 没有指定范围时，应收到真实澄清而非空白成功状态；用户必须能继续对话完成计划。
- Working interpretation: LLM 编排的澄清消息是正式中间结果，后端结果显式携带该状态/文本，聊天页只在该状态渲染澄清回答及已有快捷选项；不得生成模板计划或空白调度卡。
- Explicit non-goals / forbidden interpretations: 不为无范围请求硬编码计划；不重做聊天视觉、不改计划页/阶段卷、不会让 UI 猜测 LLM 意图。
- Scope and affected users/data/states: `clarification_needed` 草案、recommendation result、Master Chat `study_plan` 渲染；缺少范围的计划请求。
- Uncertainty and decision boundary: 无；用户已明确修复方向，沿用现有 clarification-response 样式。
- Change record: 本文上述 `clarification-response-runtime-handoff`。

## 意图、能力与证据分解

| 层 | 结论 |
| --- | --- |
| Intent | 用户能得到符合指定范围或真实学习信号的可执行计划。 |
| Capability | 服务能从真实章节/KG、学习记录和阶段卷结果生成/保存有明确范围、依据、依赖和语义的任务，并将其映射到既有学习或阶段卷链路。 |
| Evidence | 单元/契约测试验证任务结构、范围、顺序、无证据时追问和旧项兼容；真实数据运行验证单章/整书不含机械重复且每项执行入口一致。 |
| Expression | 不适用：本轮明确不改前端或视觉展示。 |

## 行为与架构映射

| Behavior | Must be observable/true | Boundary / invariant |
| --- | --- | --- |
| 单章草案 | 每次非空请求均调用 LLM，在真实范围、时间与证据约束内编排学习/复习/练习 | 一章不是按知识点或相同文本编号拆分；LLM 不得越过事实范围；失败不得由模板代替。 |
| 明确复习草案 | 用户指定“复习某书某章”时，LLM 在复习优先和真实弱点证据约束内编排复习/练习 | 不得把学习任务换标题；无弱点证据不得虚称提高或高难。 |
| 整书草案 | LLM 在服务端给出的章节顺序与真实范围内安排滚动学习、复习与练习，并在合理时安排总复习 | 不将全书平铺或压到一天；代码不得把固定第 N 日结构当成计划输出。 |
| 未指定范围 | 有真实信号才推荐；不足则保留对话上下文并要求澄清 | 不生成无证据的通用模板或虚称弱点。 |
| 任务执行 | learning/review 复用 scoped learning；基础/提高练习使用既有 stage exam 语义 | `practice` 不得静默路由为普通学习；session 完成不完成计划项。 |

| Behavior | Upstream truth | Owner | Intermediate contract | Consumer/output | Legacy path forbidden |
| --- | --- | --- | --- | --- | --- |
| 章节范围 | KG overview chapter records | `study_plan` scope resolver | 真实名称、document/chapter/KG IDs 的 `learning_scope` | 草案和正式计划项 | 假编号或无范围“复习/练习” |
| 草案语义 | 标准化计划输入和学习证据 | `study_plan` generator | 带 task type/date/reason/dependencies 的草案项 | 草案采纳 | 重复 `(i/N)` 项 |
| 复习语义 | `review_chapter` 请求、范围与已有掌握度/基础练习结果 | `study_plan` generator / Router | `review_chapter`、`advanced_practice`、前置任务与定向弱点 scope | scoped targeted learning / stage exam | 将复习或 practice 静默当普通学习 |
| 个性化编排 | 已校验 scope、候选语义、时间预算、画像和真实学习证据 | LLM planner + `study_plan` validator | 每次非空请求的 LLM 调用记录与 schema 约束的任务编排候选 | 经校验的草案项，或 LLM 生成的澄清/可重试错误 | 规则生成的模板任务，或 LLM 虚构事实 |
| 执行映射 | 正式计划项、关联 session、掌握度 | Router/orchestrator | 任务类型与 scope/session contract | scoped learning / stage-exam endpoints | `practice`→普通学习 |

## 需求—证据台账

| Requirement | Interpretation/behavior | Planned owner | Implementation status | Evidence required | Prohibited legacy result | Current verdict |
| --- | --- | --- | --- | --- | --- | --- |
| R1 真实章节范围 | 从真实 chapter/KG 记录得到名称和 IDs | `study_plan` | verified | `ResolveLearningScopeTests` 49 passed；真实 producer shape 已核验 | 假知识点编号 | verified |
| R2 三模式生成 | 单章、整书、无指定分别生成/追问 | `study_plan` | verified | 真实 provider/HTTP 矩阵覆盖无范围、单章、整书、章节集合、主题和短续答；真实聊天页覆盖澄清、薄弱点与显式章节计划的生成/修改 | 重复机械任务、通用模板或明确范围却澄清 | verified |
| R3 正式任务契约 | 保存语义、范围、依赖、session/练习层级且兼容旧项 | memory/API | verified | roundtrip/迁移/兼容测试 | 旧项不可读 | verified |
| R4 执行路由 | 四类任务映射到既有 scoped learning/stage exam | Router/orchestrator | verified | 34 项正式计划路由与状态分离测试 | `practice` 作为普通学习 | verified |
| R5 运行时验收 | 真实数据验证范围和执行入口 | Codex | verified | 正式 HTTP 提供整书/多轮/泛化覆盖；精简真实浏览器证明生成、parent 修改、采纳和 learn/review/basic 最终正文或题目 | 仅 KG/stub 测试声称端到端 | verified |
| R6 独立复习语义 | 明确复习请求生成/执行复习和提高练习，而非学习改名 | `study_plan` / Router | verified | 结构、依赖、证据与执行映射测试 | 复习=学习标题；practice=普通学习 | verified |
| R7 LLM 个性化编排 | 每个非空计划请求调用 LLM；LLM 在已验证事实/预算内个性化安排 | LLM adapter / `study_plan` validator | verified | 真实 provider 两阶段、JSON 兼容与 parent 重编排 evidence；真实页面 child draft 可见且约束变化一致 | 规则模板替代 LLM、空输出失败或模型虚构范围/证据 | verified |
| R8 澄清结果交接 | LLM 澄清消息从草案到聊天页可见 | `study_plan` / Master Chat | verified | 服务/聊天测试 + 真实浏览器正反标记 | 空建议伪装成功或空调度 | verified |
| R9 画像、掌握度与整本书入口 | “我有什么薄弱点”及“按我的薄弱点规划”必须消费真实 learner memory/profile/mastery；明确书名但未指定章节必须进入真实整书范围；前者返回可追溯概览，后两者交给 LLM 编排 | `study_plan` / MasterRouter / Master Chat | verified | 生产形态 profile/mastery、同范围直接/澄清计划、无证据负例、两个真实整书和书名短续答均经正式 HTTP；真实浏览器显示两个 KG 弱点的 4 项计划并完成 parent 修改 | 泛澄清、要求用户重复提供系统已有弱点/书名、硬编码弱点或无依据计划 | verified |
| R10 真实前端代表性链路、澄清续答与修正验收 | 按用户 2026-07-17 授权删减同构浏览器用例：真实页面覆盖澄清→弱点短续答→计划、parent 修改、显式章节计划采纳及 learn/review/basic 最终内容；同义、整书、书名/章节短续答和其它修正维度由正式真实 HTTP 矩阵覆盖；普通 learning/Direct QA 各一条 smoke | Router / `study_plan` / Master Chat / Codex runtime acceptance | verified | 截图 03、06、08–13；learn/review 正文、basic 真实题目；76 项 Node、相关 Python 707 项及全部正式 HTTP evidence；默认 DB/隔离清理证据 | mock/stub、固定关键词、后端直调、澄清页面冒充成功、重复措辞用例替代独立能力、丢失会话上下文、忽略修正或计划卡后未看到真实内容 | verified |

## 有序任务节点

1. R1：审计并补齐真实章节知识点名称的范围数据契约与测试（已独立验证）。
2. R2/R6/R7：定义 LLM 个性化编排输入/输出 contract 与服务端验证（当前）；接入已有 LLM 调用，使每个非空计划请求都经过 LLM，再实现单章、整书及明确复习请求的不同任务语义、理由和依赖。规则代码仅验证/补全事实，不得生成计划或作为兜底。
3. R2：实现未指定范围的真实信号选择与无数据澄清分支。
4. R3/R6：扩展正式计划项的统一任务契约，保存任务类型、依赖、练习层级和关联 session，并验证旧数据兼容。
5. R4/R6：按任务类型接线 scoped learning、明确复习、基础阶段卷与真实弱点范围的提高练习阶段卷。
6. R2–R4：后端回归与隔离的端到端验证。
7. R5：使用真实数据独立验收；完成后停用自动化。
8. R9：让画像/掌握度成为薄弱点查询和薄弱点计划的真实入口，并修复明确书名的整本书范围入口；先建立统一弱点证据和整书解析到范围/概览的正式 handoff，再接入 Router、LLM 编排和最小聊天展示，最后以生产形态画像、真实书名与真实聊天页独立验收。
9. R10：在 R9 修复后实施最终真实前端代表性验收。浏览器只覆盖五类独立风险入口各一条；措辞 A/B、章节名/书名短续答的泛化由已通过的正式真实 HTTP 矩阵提供，不在浏览器重复。选择三个结构不同且能覆盖每日预算/练习组合、整书期限与 scope 保留、主题或范围调整的 parent 修正；再用一个正式章节计划完成采纳和 learn/review/basic 三类真实启动。任一代表性链路失败即回退到最小生产链路修复，禁止以 mock、关键词或人工改写输入绕过。

## 变更记录

- 2026-07-15：从交接说明建立无人值守任务分解；尚无已独立验证的实现变更。
- 2026-07-15（R1 证据返工）：原 R1 任务的验收命令使用 `-k "resolve_learning_scope"`，pytest 实际选择 0 项；这不能证明范围契约。最小修订是不改产品实现，仅以精确的 `ResolveLearningScopeTests` 选择器复现并验证该独立测试类。受影响节点：R1 当前任务；后续节点不变。此修订不引入架构或数据契约变化。
- 2026-07-15（用户确认的复习语义扩展）：原 R2 假设“章节计划”的学习/复习结构足以覆盖用户意图。用户确认学习与复习生成不同，证据是其明确要求“复习某书某章”单独实现。计划修订为新增 R6，明确 `review_chapter` 与 `advanced_practice` 的生成、依赖和执行边界；影响 R2–R4/R5，R1 不变。该修订仅将交接文档第 3.2 已有的复习定义落实到任务顺序，不引入新复习引擎或新阶段卷能力。
- 2026-07-15（用户确认的 LLM 编排扩展）：原 R2 假设固定章节学习/复习/练习序列可作为主要生成策略。用户授权 LLM 生成个性化计划。计划新增 R7：LLM 只能在服务端提供的已验证候选、范围、预算和证据内编排；validator 必须拒绝越界或虚构输出。影响 R2、R6 及其后续 R3–R5 消费；R1 不变。该修订不让 LLM 直接写正式计划项，也不扩大既有阶段卷能力。
- 2026-07-15（用户禁止硬编码替代 LLM）：用户进一步明确：输入计划后肯定调用 LLM，绝不能用硬编码生成计划替代。原 R7 的“固定序列作为兜底”假设失效。修订：固定生成器仅保留为非计划的内部验证/历史兼容代码，不能处理非空计划请求；LLM 不可用、超时或不合约时必须返回可读可重试错误或由 LLM 的澄清合同处理，绝不返回模板任务。影响 R2/R6/R7、所有 LLM adapter 接线与失败测试；当前仅实现 validator 的任务不受影响。
- 2026-07-16（R8 运行时缓存返工）：R8 首轮实现已让真实 `/api/router/jobs` 完成事件携带 `clarification_needed=true` 与非空 LLM 澄清文本，但浏览器实际页面仍显示旧的“已生成学习计划建议 / 调度详情”。证据表明 `frontend/pages/master-chat.html` 固定引用旧版 `master-chat.js?v=20260712-tutor-audio`，使缓存的旧渲染器继续执行；因此先前“修改 JS 即会由页面消费”的假设失效。最小修订为只更新该页面对本脚本的版本化引用，并以其与渲染器的加载契约做回归验证；不新增接口、不改其他页面、路由或计划语义。R8 降级为 failed，待缓存入口和真实浏览器正反标记重新验证。
- 2026-07-16（R8 同路径缓存二次返工）：入口查询版本已更新且静态断言通过，但新的真实浏览器 tab 仍渲染旧成功卡；同时同一 `demo` 用户的真实 job snapshot 已证明 `clarification_needed=true` 与非空澄清消息到达完整 payload。由此否定“更改 query string 足以让当前浏览器取新字节”的假设。最小架构修订是将唯一正式入口改为新的版本化脚本**路径**，通过一次受控 rename 保持单一源文件，不保留平行脚本或 renderer fallback；更新唯一 HTML 引用和 Node 测试加载路径。R8 继续为 failed，直至真实聊天页显示正向澄清标记并且旧成功/调度标记缺席。
- 2026-07-16（R8 HTML 缓存根因修正）：唯一脚本路径任务后，浏览器 DOM 仍明确显示旧 HTML 内的 `/js/master-chat.js?v=20260712-tutor-audio`，而本地 HTTP 读取已证明服务器文件是新入口；因此实际根因不是 JS 查询或同路径脚本缓存，而是 `static_fallback` 未对 HTML 页面发送禁止缓存的响应头。上一步的 JS rename 是错误的中间假设，必须撤回。最小修订：将脚本恢复为单一原路径并保留查询版本更新；仅为 HTML `FileResponse` 加 `Cache-Control: no-cache`，并以响应头测试和真实浏览器重新加载验收。不得为该问题保留版本化副本、新静态模块或双维护路径。
- 2026-07-16（R9 画像/掌握度与整书入口纠正）：用户以真实聊天页分别提出“给我计划一下我的薄弱点怎么学”和“我有什么薄弱点”，页面却分别要求用户再次列出薄弱点、或落入泛意图澄清；随后明确请求“学习牙体材料学”和“牙体材料学整本书”，仍被要求指定章节。此前 R7/R8 的错误假设是：只要在“已指定章节/整本书”候选中标注弱点证据，就已经消费了画像/掌握度，且书名可解析时整书分支自然可用。截图证明两者均不成立：无范围的薄弱点入口没有把 `learner_memory`、`profile_state.weakness_summary`、低掌握度和其 KG 对应范围作为正式上游事实；可解析书名也未稳定进入整书候选。本修订新增 R9：统一由真实画像/掌握度生成可解释弱点概览和可校验范围；“查薄弱点”不得再走泛澄清，“按薄弱点计划”必须把该范围交给 LLM；可解析书名无章节必须按整书候选编排。只有证据或书名确实不可解析时才允许解释性澄清。不得虚构主题或要求用户重复系统已有数据。保留既有 MasterRouter intent、一般学习、阶段卷和非相关页面；仅允许为该正式结果做最小聊天展示。
- 2026-07-16（R9 多轮计划会话根因扩展）：用户在系统要求说明《牙体材料学》章节后，明确回复“粘合剂和牙科陶瓷材料”，仍被当成无上下文的新意图并返回泛澄清。该运行时证据否定“单条消息足以承接计划澄清”的假设，也解释了书、章节和薄弱点入口均不稳定的共同根因。R9 扩展为：正式聊天请求必须把最近的计划澄清/用户补充历史传入 Router 分类及计划解析 LLM；解析出的书名、章节和用户补充必须以同一条可追溯会话链合并，再由真实 KG/画像约束。不得依赖截图句子或单轮关键词；历史缺失才可解释性澄清，且不得丢弃已在同一会话明确的范围。
- 2026-07-16（R2/R5/R7 真实正向验收撤销）：用户用真实聊天页证明“学习《牙体材料学》”“整本书”“补充粘合剂和牙科陶瓷材料”均未生成计划；另一个明确单次主题请求显示 `LLM orchestration returned empty or non-string output`。这否定了此前将真实 KG provider 与受控 LLM stub 视为真实用户端生成验收的假设。R2、R5、R7 从 verified 降级为 failed；R8 只证明澄清可见，仍保持其窄范围 verified。后续任何完成声明必须包含真实 provider 第二次编排调用的可观察成功证据，以及浏览器中明确单章、整书和多轮补充的成功计划卡片；不得将 mock、KG scope 或澄清页面替代正向生成验收。
- 2026-07-16（用户指定 R10 最终硬验收）：用户明确要求：先在真实页面输入“给我生成学习计划”，LLM 必须进一步自然询问；随后“生成关于我薄弱点的计划”必须基于真实画像/掌握度生成。关于某本书、某章节、以及“某本书 + 某章节 + 某疾病/主题”的计划都必须生成；匹配应理解同义、语序、口语化描述与主题表达，不能依赖固定句式或精确章节字符串。五类情形各必须使用两种完全不同表达询问，并在前端逐次确认生成内容正常、含学习/复习/练习语义。每次初版后，用户还要提出一个不同维度的修正要求，系统必须在同一真实会话中重新生成符合修正的新计划。此要求不允许 hardcode、mock/stub、仅后端调用、手工改写用户输入或将澄清视作成功；任一场景或修正失败，R2/R5/R7/R9/R10 均不得标记成功。
- 2026-07-17（用户授权删减明显重复的浏览器验收）：用户明确允许减少明显重复的真实浏览器用例。工作解释是：已经由真实 Provider + 正式 HTTP 证明的同义 A/B、两种教材同结构表达、章节名/书名短续答泛化，不再要求 Codex 在浏览器逐句重复；浏览器改为覆盖五个独立风险入口各一条、三个不同 parent 修正维度，以及一个采纳后 learn/review/basic 三类真实启动。不得删减的底线是：薄弱点真实证据、整书多章、单章、书+章节+主题、多轮短续答均至少有一条页面证据；至少一次修改保留 parent/scope；至少一个正式计划点击开始后页面看到真实学习/复习正文和基础练习题；普通 learning 与 Direct QA 各一条轻量 smoke。原 11+11 数量门槛由本记录明确取代，能力边界、正反标记、真实 Provider、禁止 mock/hardcode 和 Codex 亲自浏览器操作要求不变。
- 2026-07-16（R9 首轮独立复核）：任务 `task-20260716-082529` 的 runner 因 SessionEnd hook 取消而记录 exit code 3/failed；不能采信其完成报告。Codex 独立运行计划相关 Python 459 项与 Master Chat Node 22 项均通过，且确认代码新增画像弱点概览、`review_weakness`→真实 KG scope、书名 whole-book、history handoff 与 JSON mode 请求，因此 R9 只能到 implemented。该证据不含真实 provider 的第二次非空 JSON、真实 `/api/router/jobs` payload 或浏览器正向计划卡，故 R9 verdict 为 cannot-prove，R2/R5/R7 保持 failed，R10 保持 designed。下一任务必须首先诊断/修复真实 provider 空输出并取得最小真实页面正向生成证据；不得以受控测试替代。

## R10 最终真实前端代表性验收矩阵（2026-07-17 用户授权精简后的硬门槛）

前置条件：使用本地真实聊天页、当前实际加载的 HTML/JS、真实 provider；对“薄弱点”场景以现有生产形态学习记录 API/存储准备一个隔离验收用户的真实 `weaknesses`、低 `mastery` 与可解析 KG IDs，不能在前端、LLM prompt 或测试 renderer 写入弱点。每一轮保留 request/job payload、真实第二次编排调用的非空 JSON 证据、页面截图及正反标记。测试之间使用独立会话或明确重置上下文，避免一个案例替另一个案例提供范围。

| 类别 | 浏览器代表表达 | 已由正式 HTTP 承担、无需浏览器重复的泛化证据 | 必须在真实页面观察到 | 禁止结果 |
| --- | --- | --- | --- | --- |
| 1. 无范围→薄弱点短续答 | `给我生成学习计划` → `我掌握得不好的那些内容` | 已通过薄弱点直接入口、短续答同范围和无证据负例的正式 HTTP 矩阵 | 先显示可读澄清，再从真实画像/掌握度生成计划卡；对应 parent 修改后生成新卡 | 空成功卡、泛澄清、要求用户重列系统已有弱点、虚构弱点 |
| 2. 整本书计划 | `帮我规划整本《<真实教材名>》` | 两种结构不同整书表达及书名短续答已由正式 HTTP 验证 | KG 多章范围和跨章节计划卡；同会话期限修改后仍保留整书范围 | “请指定章节”、只选一章、修改静默丢章 |
| 3. 单章计划与正式启动 | `学习《<真实教材名>》第<真实章节号>章` | 两本教材的 scoped-learning 与章节标题短续答已由正式 HTTP 验证 | 唯一 chapter/document；采纳后依次启动 learn/review/basic，并看到真实正文与题目 | 泛澄清、错误 scope、空内容、basic 静默走普通学习 |
| 4. 书+章节+疾病/主题 | `制定《<真实教材名>》第<真实章节号>章关于<真实疾病/主题>的计划` | 两种语序/主题结构已由正式 HTTP 直达矩阵验证 | 书、章节与 focus 同时保留；主题/范围调整后新草案可见 | 忽略主题、硬编码教材词、修改丢失原 scope |
| 5. 书名或章节名短续答 | 无范围请求后仅回复一个真实书名（优先）或真实章节标题 | 章节名和书名两类短续答均已有正式 HTTP child-draft 与会话隔离证据 | 页面消费当前计划澄清状态并生成正确计划卡 | 把短续答当新话题、要求重复“制定计划” |

### R10 通用不变量

- 同义表达的泛化仍必须由已有正式 HTTP 矩阵证明；浏览器只保留一个代表表达，不再重复只改变句式/词序的页面操作。
- 范围解析由真实 KG + LLM 语义解析完成；确定性代码只能验证候选、预算、依赖、权限和 evidence provenance，不能生成计划、固定回复或规则排程。
- 每个可生成场景必须实际触发两阶段 LLM：解析与编排；第二次响应必须是非空、可解析、经 validator 接受的 JSON。对 provider 空/异常，页面必须出现可读、可重试错误，且不得伪造计划。
- 每个成功计划的所有任务不得声称不存在的能力：学习/复习复用既有 scoped learning；练习复用既有基础/提高阶段卷语义；提高练习仅能使用真实弱点证据。计划至少展示其实际编排的任务类型；测试集合整体必须覆盖学习、复习和练习，不以标题改名充数。
- 浏览器验收必须对上述 5 条代表链逐案检查正向计划标记与反向标记（无空“调度详情”、无泛澄清、无内部异常、无错误“请指定章节”）；截图直接展示给用户。另需 3 条不同维度 parent 修正、1 条采纳后 learn/review/basic 三类真实启动，以及普通 learning/Direct QA 各 1 条轻量 smoke。缺少其中任一独立能力证据不得宣布完成或停用自动化；无需重复已由正式 HTTP 证明的同义 A/B 页面用例。

### 2026-07-16 产品决定：首次章节计划统一确定性生成

- 此决定覆盖上文“每个初版必须实际触发两阶段 LLM”的通用要求，适用于首次生成且已解析出一个或多个真实章节的章节计划，包括单章、章节集合/区间及整本书。语义识别和章节集合解析仍由 LLM + 真实 KG 负责；确定性代码只消费已经校验的有序章节 scope，不用关键词穷举判断“前三章/最后几章/第几章和第几章”。主题、疾病或薄弱点若不能可靠映射为真实章节 scope，仍走原 LLM 编排或具体澄清，不得伪装成章节计划。
- 首次章节计划按真实 KG 的有序章节 scope 统一生成，不调用 orchestration LLM。对第 i 章：其进入日安排 `learn_chapter + review_chapter`，下一日安排一次 `basic_practice`；多章时流水交错为第 1 天学习+复习第 1 章，第 2 天练习第 1 章并学习+复习第 2 章，以此类推，最后一章的下一天只完成其基础练习。每天最多引入一个新章节，每章恰有一次学习、一次复习和一次基础练习，不生成 `advanced_practice`/精练。不得硬编码教材名、章节数、标题、KG IDs 或示例书。
- 本产品决定不新增任何 `advanced_practice` 权限例外：其他 LLM/单章/主题/薄弱点路径保持既有弱点证据规则，首次整书确定性计划也只使用 `basic_practice`。
- 代码必须按每日预算有界分配分钟数并保持正整数、合法依赖和真实可执行 scope；预算不足以容纳当日任务时返回具体冲突，不得静默删任务、删章或突破预算。N 章初版默认占 N+1 天。用户后续要求期限、每日时间、任务顺序、侧重、范围或练习量等修正时，必须以首次草案为 parent 调用真实 LLM 重编排，而不是继续使用确定性模板。
- 首次确定性草案仍必须使用正式 draft/item contract、四类执行接线和前端计划卡；所有 item 保存前必须通过资源可执行性 preflight。验收需覆盖直接单章、多个连续章节、离散章节及整本书入口，并在真实页面实际启动学习、复习、基础练习；还要在同会话证明后续不同维度修正真实调用 LLM、生成不同 draft，并继承原章节 scope，除非用户明确改变范围。

### 无范围澄清后的三种续答（正式 HTTP 三类全测；浏览器代表性抽测）

三类均必须由正式真实 HTTP 会话证明 parent/history 承接、正确 scope 和 child draft；该证据已完成。浏览器按 2026-07-17 用户授权精简为：薄弱点续答必测，再从章节名/书名中选择一条代表性页面复验（优先书名，以覆盖整书短续答）；不再把两种已由 HTTP 证明的短续答都重复跑页面。第二句仍不得重复“计划”“生成”“学习安排”等动词。

| 续答类别 | 首句无范围请求 | 用户澄清后的唯一续答（自然、模糊、不重说计划） | 必须承接的真源 | 正向结果 | 反向结果 |
| --- | --- | --- | --- | --- | --- |
| 薄弱点 | `帮我把接下来的学习安排好` | `我掌握得不好的那些内容` | 隔离验收用户已有 `weaknesses`/低 `mastery`/profile evidence 与可解析 KG IDs | 从真实弱点范围调用 LLM 并显示计划 | “不太确定您的意图”、要求重新说明要做计划、要求重复列薄弱点 |
| 章节名 | `给我做一个学习安排` | `<真实教材中某章节的标题>` | 上一轮计划澄清状态 + 真实 KG 章节候选；不得要求用户再重复书名或计划意图 | 解析对应 chapter/document/KG scope，调用 LLM 并显示计划 | 把章节名当 Direct QA/泛澄清、要求重新说“制定计划” |
| 书名 | `我接下来应该怎么规划学习` | `《<真实教材名>》` | 上一轮计划澄清状态 + 真实 KG 同书多章节候选 | 展开真实整书范围，调用 LLM 并显示跨章节计划 | “请指定章节”、把书名当新话题、只生成单章 |

三类正式 HTTP 证据中，第二句不能加入任何专门为测试准备的关键词；浏览器代表性用例同样不得人工补写计划动词。保留实际 request/job payload、解析出的 scope 和页面正反标记；不得以历史拼接字符串或固定回复替代正式会话状态、LLM 解析与 KG 验证。

## R10 第二阶段：代表性用户修正后的真实前端重编排（精简后硬门槛）

选择三个结构不同的初版成功场景，在**同一聊天会话**各输入一条用户修正：薄弱点计划验证每日预算+练习量，整书计划验证期限且 scope 不丢章，书+章节+主题计划验证 focus 或范围调整。系统必须保留原计划的范围、用户身份和已确认事实，将修正作为新的约束交给真实 LLM 重编排；不得把修正当新话题、只回复文字确认、复用旧卡片或用规则改标题。其他修正维度由自动合同和正式 HTTP 证据承担，不逐条重复浏览器操作。

修正能力全集仍按下表保持自动合同覆盖；浏览器只从中选择三条代表组合，不要求十条逐一页面复验：

| 修正维度 | 用户修正的可验证意图 | 新计划必须可观察的变化 | 禁止伪通过 |
| --- | --- | --- | --- |
| 1. 学习期限 | 缩短或拉长完成天数 | 日期跨度/日程分布符合新期限 | 只改说明文字或保留旧日期 |
| 2. 每日可用时间 | 调整每天分钟数 | 每日总分钟不超新预算，任务数量/粒度相应重编排 | 旧预算或只改总时长显示 |
| 3. 学习与复习侧重 | 要求更多新学或更多巩固 | task type、顺序和理由体现新比例 | 只替换标题中的“学习/复习” |
| 4. 练习诉求 | 要求增加基础练习或在真实弱点成立时提高练习 | 计划含合法练习语义与依赖，且不越过既有阶段卷能力 | `practice` 静默变普通学习或虚称高难 |
| 5. 范围收窄 | 只保留某章节/主题，排除其他内容 | scope candidates 与每项 `learning_scope` 仅留允许范围 | 旧书/旧章节仍混入 |
| 6. 范围扩展 | 从章节扩大到整书或加入相关主题 | 真实 KG 可解析范围扩大且计划重新分配 | 只把旧章重复多天 |
| 7. 薄弱点优先级 | 要求优先处理某个已证实薄弱点 | 有证据的弱点更早出现，理由引用真实 evidence | 伪造/硬编码弱点或忽略优先级 |
| 8. 节奏/间隔 | 要求复习间隔更密或更疏 | day offsets、依赖和复习时点变化且仍合法 | 保留旧顺序/日期 |
| 9. 目标/考试导向 | 要求按近期考核、临床应用或知识掌握目标组织 | focus/reason/任务组合响应目标，但不虚构考题/能力 | 仅改一行文案 |
| 10. 任务顺序偏好 | 要求先练后学、先总览后细节等合法顺序偏好 | 合法 dependency 与任务顺序明确改变 | 违反依赖、预算或保留旧顺序 |

修正验收的额外反向断言：不得丢失初始书/章节/主题事实；不得以“无法理解修正”泛澄清（除非修正与真实范围/已有能力冲突，且须具体说明冲突）；不得显示旧计划为最新结果。没有 3/3 代表性修正成功、parent/child IDs 与可观察变化一致，不得关闭 R10、不得将 R2/R5/R7/R9 标回 verified、不得停用自动化。

## 2026-07-16 真实 provider 兼容返工记录

- 原节点/假设：R9 真实 provider 运行时返工假设空输出只发生在第二次 orchestration，并通过关闭该次 `response_format` 即可取得可泛化的正向生成。
- 失效证据：Codex 独立复跑 459 项 Python 与 22 项 Node 测试通过；真实 Master Chat 对“口腔临床药物学中药物不良反应检测”成功生成计划，但结构不同的“牙体材料学里粘接剂”请求在第一次 parse 调用仍显示 `LLM returned empty or non-string output`。因此一个成功样例不能证明 provider 两阶段稳定可用。
- 精确变更：把当前结果归类为实现/证据失败；下一最小任务仅在 provider transport 与两阶段调用边界实现显式的 JSON-mode 兼容策略：先请求 JSON object；仅在可识别的空内容/不支持响应时用同一真实 LLM、同一 prompt 无 `response_format` 重试，并仍由既有 JSON extractor/validator 拒绝非法内容。禁止模板、固定回复或按教材/措辞分支。
- 受影响节点：R2/R5/R7 继续 `failed`，R9 继续 `implemented/cannot-prove`，R10 继续 `designed`；不得因首个正向页面样例升级状态。
- 兼容/迁移：不改草案、正式计划项、Router 或前端 contract，不迁移历史数据；只增强 provider 请求兼容与安全错误分类。
- 验收证据：至少两个句式和教材/主题均不同的真实前端请求连续成功；每次真实 parse 与 orchestration 均获得非空、可解析且 validator 接受的 JSON，并保留错误负例。无用户决策项。

### 2026-07-16 JSON 兼容重试超时修正

- 原假设：对 parse 与 orchestration 各自先发 JSON-mode 请求、空内容后各重试一次，可以在真实页面稳定完成。
- 失效证据：470 项 Python、22 项 Node 与 diff check 独立通过；重启当前服务后，真实前端请求“牙体材料学里粘接剂”超过三分钟仍停留后台运行。当前 transport 单次默认超时 180 秒，两阶段各自重新探测 JSON mode，最坏会串行等待四次 provider 调用。
- 精确变更：下一任务只修 provider 兼容探测的有界性和请求内共享状态；区分 timeout/unsupported/empty 与认证、HTTP、非法响应，禁止对不可重试错误盲目重试。一次计划流程确认 JSON mode 不可用后，后续阶段直接使用同一真实 LLM 的非 `response_format` 请求，同时仍经 JSON extractor/validator。
- 影响：上一兼容任务判为证据失败；R2/R5/R7 保持 failed，R9 保持 implemented/cannot-prove，R10 保持 designed。
- 兼容性：不改前端轮询、Router/job contract、持久化或外部配置；不得通过延长前端等待时间掩盖 provider 阻塞。
- 验收：自动测试覆盖超时、不可重试错误、一次探测上限和两阶段共享；重启服务后两个结构不同真实请求均在有界时间内生成计划或返回明确可重试错误，不能无限“后台运行”。

### 2026-07-16 Provider 兼容里程碑独立通过

- 独立证据：481 项 Python、22 项 Node 与 `git diff --check` 通过；无新 commit 或 `docs/project-notes.md` 改动。重启真实服务后，两个全新 Master Chat 会话分别以“牙体材料学粘接剂”与“口腔临床药物学药品不良反应监测”的不同句式，在约 95 秒与 60 秒内生成不同 draft（`draft_2df3b65fd7ce999ab3973f3e44ea2a22`、`draft_e36e88b39675573e830f9a10fdb7dc5e`），页面显示学习/复习/练习任务和“打开学习计划”，无 empty/non-string、泛澄清或持续后台。
- 状态边界：仅 provider 两阶段有界兼容达到 verified；R2/R5/R7/R9/R10 的完整自然表达、画像、整书、多轮和修正证据仍未完成，不升级其整体 verdict。
- 后续最小节点：从 R10 无范围澄清三分支中先完成“只回复章节名”以及同会话“每日时间预算修正”的一个完整初版+修正场景；不得批量跳过真实前端验收。

### 2026-07-16 R10 章节续答首轮真实页面失败

- 自动证据：首轮实现后的 487 项 Python、22 项 Node 与 `git diff --check` 独立通过；实现了父草案范围继承与前端 draft ID 持久化。
- 真实反证：重启当前服务后，同一真实 Master Chat 会话中“帮我安排一下接下来怎么学”正确产生 LLM 澄清，但用户随后仅回复真实章节标题“第三章 牙周病的发病机制”时，MasterRouter 返回“请告诉我您想做什么”，并显示调度详情；没有进入计划解析或生成草案。
- 判定：实现失败。首轮任务只补了下游父草案继承，未让 Router 基于正式计划澄清 history/context 将简短范围续答分类为 `study_plan`；因此不能进入预算修正验收。
- 精确返工：只修正式计划澄清状态下的 Router 续答分类/handoff；普通无上下文章节问题必须保持既有分类。完成后仍须在同一真实页面复验澄清、仅章节标题生成、20 分钟重编排三个回合，不能用单测或后端直调代替。

### 2026-07-16 R10 章节续答第二轮真实页面失败

- 自动证据：Router 返工后的 492 项 Python、22 项 Node 与 `git diff --check` 独立通过；结构化 `parent_plan_draft_id` 与父草案 `clarification_needed` 已使第二句进入 `study_plan`。
- 真实反证：同一重启后的真实 Master Chat 会话中，首轮澄清后仅回复“第三章 牙周病的发病机制”，系统不再显示 Router 泛澄清，但 study-plan 仍再次要求用户指定教材和章节，没有生成计划卡。
- 判定：实现失败已从 Router 入口收窄到计划解析/KG 候选消歧。当前真实章节标题未被解析为唯一合法 chapter/document scope，或解析结果在 validator 前丢失。
- 精确返工：诊断真实 parse JSON、候选集合与 validator；对于可唯一解析的真实章节标题，由 LLM 语义解析并经 KG 候选校验后生成计划，只有零个或多个合法候选时才允许具体澄清。禁止硬编码教材、章节、示例文本或精确字符串专用分支。

### 2026-07-16 R10 无范围章节续答与每日预算修正独立通过

- 证据纠正：前两次页面复验使用的“第三章 牙周病的发病机制”并非当前真实 KG 标题；真实 provider 共 227 个章节，牙周病学第三章为“第三章 牙周病的分类和流行病学”。该错误样例不能作为产品失败证据，已改用真实 KG 动态证据重验。
- 自动证据：498 项 Python、22 项 Node 与 `git diff --check` 独立通过；无 commit、push 或 project-notes 改动。
- 真实页面：同一新会话中“帮我安排一下接下来怎么学”产生 LLM 澄清；仅回复真实章节标题后生成 `draft_29c4defeb87e8619722501282668408b`，包含学习、复习和基础练习；随后“我每天最多只能学20分钟，重新排一下”生成不同草案 `draft_9948e12e5fa890e7218bb1c59279366a`，范围仍为同一真实 chapter/document，所有任务均为 10–20 分钟。
- 状态边界：仅 R10 的“无范围→章节名续答”初版和修正维度 2 达到 verified；R10 整体、R2/R5/R7/R9 不升级。下一节点验证无范围澄清后的书名续答与不同修正维度。

### 2026-07-16 R10 无范围书名续答首轮真实页面失败

- runner 边界：任务 `task-20260716-112949` 因 SessionEnd hook cancelled 记录失败；其 502+22 通过陈述不作为验收依据。Codex 独立确认新增 4 个书名/期限聚焦测试通过、Master Chat Node 22 项通过且 `git diff --check` 通过；全量指定 Python 命令在 120 秒验收门限内未返回，不能据此升级状态。
- 真实反证：重启服务后，在全新真实 Master Chat 会话输入“我接下来该怎么规划自己的学习？”得到 LLM 澄清；随后只输入真实教材名“《牙周病学》”已正确进入后台学习计划任务，但约 120 秒后终态为 `学习计划推荐失败：Failed to parse orchestration JSON: Unterminated string starting at: line 128 column 7 (char 3310)`，没有计划卡，无法继续期限修正。
- 判定：实现失败。正式会话承接、Router 分类和整书 KG 候选已到达 orchestration；当前最小根因为多章节上下文对应的长 orchestration 输出被截断/不完整，既有 JSON extractor 正确拒绝而没有伪造计划。受控测试的短 JSON 未覆盖真实 provider 长输出上限。
- 精确返工：仅修复真实 provider 整书 orchestration 的有界输出契约与截断恢复。优先约束返回字段/任务数量/输出 token 预算，并只对可识别的截断或不完整 JSON 做至多一次同一真实 LLM 重试；重试必须要求更紧凑的完整 JSON，仍由既有 validator 拒绝非法 scope、日期、预算和依赖。禁止字符串补括号、模板计划、代码生成日程、教材名/固定问法分支或无限重试。
- 验收：自动测试覆盖截断、一次重试上限、非截断非法 JSON不重试、两本真实多章教材；随后重启服务，在真实页面重新完成无范围澄清→仅书名→整书多章计划→一周期限修正，并保存两个不同 draft、真实多章 scope、日期变化及学习/复习/练习证据。仅该完整场景通过后才可升级此 R10 子节点。

### 2026-07-16 R10 整书截断修复通过、范围与任务组合验收失败

- runner 边界：`task-20260716-115903` 记录 exit code 3，runner 报告不作为验收依据。Codex 独立审查新增截断分类/紧凑重试且确认共用一次重试预算；独立回归为 515 项 Python、22 项 Node、`git diff --check` 全部通过。
- 截断里程碑：重启服务后的全新真实页面会话以“最近学习有点乱，帮我理一理接下来怎么安排？”触发 LLM 澄清，仅回复“《口腔颌面外科学》”后成功生成 `draft_4fc2352cddf612f537785d3217de684d`；页面覆盖真实 14 章且不再出现 incomplete/unterminated JSON。因此“整书长 JSON 有界截断恢复”子问题达到 verified。
- 新真实反证：初版 14 项全部为学习语义，没有复习或练习；同会话输入“时间有点紧，压缩到四天内完成，重新安排一下。”生成新草案 `draft_9350307b7974dd4d10e668c4bfedc4fc`，但只保留第 1、2、3、4、6、7 章并重复第 4、6 章，静默丢失其余整书章节；页面虽出现回顾语义，仍无基础练习。截图已保存于本轮真实浏览器证据。
- 根因/判定：实现失败已收窄为 orchestration 内容合同不足。当前 validator 只检查返回的每个 `scope_ref` 属于候选集合，不要求 whole-book 请求覆盖全部候选，也不要求整书计划具备学习、复习、基础练习组合；紧凑 prompt 的 `total_days × 2` 上限又会在 14 章 + 巩固任务时与全范围覆盖冲突。不得把“合法子集”误认作整书计划。
- 精确返工：在 context 中显式携带由已验证请求推导的 whole-book coverage/composition 要求；LLM prompt 负责生成覆盖全部真实 scope 且包含 `learn_chapter`、`review_chapter`、`basic_practice` 的计划，确定性 validator 只拒绝缺失范围/类型的候选并允许一次有界内容重试，不能补任务或生成日程。修正请求继承相同整书不变量；任务上限需按 scope 数量与必需巩固项动态计算，仍受输出和预算硬约束。
- 状态边界：仅截断恢复 verified；无范围书名续答+期限修正场景仍 failed，R10 整体及 R2/R5/R7/R9 不升级。

### 2026-07-16 整书确定性例外导致任务边界变更

- 用户明确授权“首次整本书计划”不调用 LLM，按固定章节推进语义由代码生成；后续修正仍调用 LLM。随后明确取消精练，因此确定性初版只生成学习、复习和每章一次基础练习，不新增 `advanced_practice` 例外。该产品决定替代上一返工任务对“整书初版必须由 LLM 生成”的要求。
- `task-20260716-123504` 在产品决定到达前已启动，随后 PID 死亡且 runner recovery 记录 exit code 3；其部分实现新增 whole-book coverage/composition context、prompt、validator，可作为后续 LLM 修正的待审查工作树事实，但任务整体不得验收通过。
- 下一最小任务必须先审查并整合该部分实现，然后增加唯一确定性首次整书生成器、服务端来源标记、预算/依赖验证和初版/修正分流；不得生成精练或放宽高级练习权限，禁止回退已验证的长 JSON 截断恢复。

### 2026-07-16 新增真实反例：章节集合表达、单章紧凑度与任务可执行性

- 单章紧凑度反例：真实页面仅输入“牙周病学第三章”后生成 13 个任务、总计 210 分钟；同一章节被拆成大量 5–25 分钟小项。判定为内容合同失败：单章默认计划缺少紧凑度上限。默认单章应由真实 LLM 生成 3 个核心任务（学习、基础练习、复习）；只有用户明确要求多天强化、更多练习或更长周期时才允许扩展，且仍需受动态上限约束。
- 集合范围反例：用户请求“前三章”失败。根因不是缺少“前三章”关键词，而是现有解析合同只能表达单一 `chapter_key` 或整本 `book_title`，不能正式表达章节集合。不得为“前三章、最后几章、第几章和第几章”等逐句增加分支。
- 统一范围契约：新增结构化章节选择器，由 LLM 将自然语言语义解析为选择事实，确定性代码只在真实 KG 教材顺序上求集合并验证。至少支持连续区间（前 N、后 N、第 A 到 B）、离散列表（第 A、C、F 章）、标题/序号混合以及明确排除；解析结果统一成为有序 chapter scope 列表。越界、跨书歧义或空集合必须具体澄清，禁止猜测、全书回退或关键词穷举。
- 可执行性反例：真实页面对已生成的“第三章 牙周病的分类和流行病学”计划执行“开始选中的学习计划”后报 `plan_lesson scope_no_resources`，说明计划 item 的 scope 在真正 lesson executor 中没有可用 KG nodes，且知识库无匹配 evidence chunks。看到计划卡不等于成功。
- 执行硬门槛：草案保存/正式化前对每个 item 做与真实执行器一致的资源预检。`learn_chapter`/`review_chapter` 必须携带可解析的真实 chapter/document/KG scope，并至少有可用 KG node 或 scoped evidence chunk；`basic_practice` 必须能接入既有基础阶段卷。不得回退到全局无范围学习，不得保存点击后必失败的任务。
- 真实页面验收升级：每个成功场景不仅检查计划卡，还必须在真实页面实际选择并启动计划任务；至少覆盖学习、复习、基础练习三类入口，并看到进入对应真实学习/阶段卷流程。页面出现 `scope_no_resources`、空内容、错误页或静默普通学习均判失败。自动 preflight 需覆盖全部生成项，浏览器逐类型取样不能替代全部 item 的服务端可执行性验证。
- 后续顺序：先建立统一 LLM scope 解析/真实知识库落地合同，再让章节与薄弱点首次确定性排程只消费该统一 scope；随后关闭任务可执行资源闭环并执行完整真实页面复验。不得通过前端模板、原始文本关键词或各类型专用解析器绕过。

### 2026-07-16 变更请求：所有首次章节计划统一流水排程

- Trigger/source: 用户将此前“仅首次整书允许确定性生成”扩展为所有已解析章节的首次计划，并明确首次计划采用固定逐章逻辑，计划调整仍调用 LLM。
- Desired outcome: 单章、多章和整书首次计划均稳定、简洁且可执行，不再因 orchestration LLM 输出长度或自由拆分产生 13 项单章计划；多章按每天引入一章的节奏交错推进。
- Current observed problem or limitation: 当前活动任务只实现整书例外，单章仍由 LLM 生成过量任务；章节集合尚缺统一 selector，已生成 item 还存在 `scope_no_resources`。
- Scope: 首次且已解析为有序真实章节 scope 的学习计划；覆盖直接请求与无范围澄清后的章节/书名续答。
- Preserve: MasterRouter 语义分类、LLM 章节 selector、真实 KG 顺序、预算/依赖/权限校验、正式 draft/item contract、现有四类执行链路；所有已有计划的后续修正继续调用真实 LLM。
- Replace/remove: 替换“只有整书首次计划确定性生成”和“单章默认由 LLM 生成 3 项”的旧边界；禁止按原始措辞枚举章节表达，也禁止对已有草案继续套确定性模板。
- Compatibility/migration: 既有已保存草案不迁移；新规则只作用于新首次草案。已有 parent items 的修正仍按既有 LLM 合同处理。
- Acceptance evidence: 至少用两本不同教材覆盖单章、连续多章、离散多章和整书；逐项验证 N 章产生 N+1 天、每天最多引入一章、每章恰有 learn/review/basic_practice 各一次、无 advanced_practice；真实前端实际启动三类任务；每个初版再提出不同修正并证明调用 LLM、生成新草案且内容符合修正。
- Unknowns requiring user decision: 无。按用户“交错进行、每天一章”解释为相邻章节流水：第 2 天既完成上一章练习，也开始下一章学习+复习；最后一天完成末章练习。

### 2026-07-16 变更请求：薄弱点首次计划确定性生成

- Trigger/source: 用户明确指定薄弱点计划的首次生成逻辑：第 1 天复习+练习第 1 个薄弱点，第 2 天复习+练习第 2 个薄弱点，以此类推。
- Desired outcome: 用户要求“按我的薄弱点制定计划”时，系统直接消费真实画像/掌握度证据并稳定生成逐日可执行计划，不再要求用户重复列举薄弱点，也不依赖首次 orchestration LLM 的不稳定长 JSON。
- Current observed problem or limitation: R9 目前只有画像/掌握度入口代码，真实页面尚未证明可生成并启动薄弱点计划。
- Scope: 首次且由真实 learner memory/profile/mastery 解析出一个或多个可执行薄弱点的计划；覆盖直接请求和无范围澄清后的模糊“薄弱点”续答。
- Preserve: 薄弱点必须来自真实画像/掌握度并带 provenance；范围必须映射到可执行 KG/evidence scope；基础练习继续走既有阶段卷；无证据时具体说明，不得虚构。已有计划的后续调整继续调用真实 LLM。
- Replace/remove: 替换“薄弱点首次计划也必须由 orchestration LLM 自由编排”的旧边界；禁止固定薄弱点名称、排序样例、关键词列表或无证据模板。
- Compatibility/migration: 不迁移既有草案；只影响新首次草案。parent 已有 items 时不得命中确定性生成器。
- Acceptance evidence: 至少两组结构不同的真实生产形态画像/掌握度记录；N 个薄弱点必须生成 N 天，每天恰好 `review_chapter + basic_practice` 对应一个薄弱点且 provenance/scope 可追溯；真实前端实际启动复习和练习；无证据负例不生成；同会话提出修正后必须调用 LLM 并生成符合修正的新草案。
- Unknowns requiring user decision: 无。按用户原话，每个薄弱点一天，首次生成不安排 `learn_chapter` 或 `advanced_practice`。

### 最终统一闭环门禁：所有支持的计划都必须真实可用

- “完成”只允许基于真实 Master Chat 前端的完整三段链路：首次生成成功并显示合理计划；用户随后以自然语言提出修改且系统真实调用 LLM、生成符合要求的新草案；用户选择计划任务后能进入既有学习/复习/基础练习执行链路。
- 该门禁覆盖所有正式支持的计划入口和 scope 类型：单章、连续/离散多章、整本书、画像/掌握度薄弱点、明确主题或疾病，以及无范围澄清后承接上述范围。首次章节/整书和薄弱点可以使用已确认的确定性排程；主题/疾病等其他首次计划仍由真实 LLM 编排，但最终三段链路要求完全相同。
- 每类至少使用结构、措辞和范围不同的真实请求验证，不得只匹配固定句式、硬编码教材/章节/疾病/薄弱点或复用手写草案。修改维度必须覆盖期限、每日时间、顺序、侧重、范围、练习量等不同方面；修改后不能丢失未被用户要求删除的 scope。
- 计划卡可见、API 返回成功、mock/stub 测试、KG scope 存在或 LLM 调用日志均不能单独作为完成证据。必须核对页面可见内容、draft/source/scope、LLM 修改产生的新 draft，以及任务实际启动后的运行时结果；出现空输出、泛澄清、`scope_no_resources`、错误阶段卷路由或不可执行项均为整体失败。

### 2026-07-16 变更请求：统一 LLM 学习范围解析与模糊实体落地

- Trigger/source: 用户反复明确要求书、章节、知识点、疾病等学习内容不能分别写一套识别逻辑，也不能要求输入与真实名称完全一致；必须统一由 LLM 理解比较模糊的自然语言。
- Desired outcome: 用户以简称、近义表达、不完整标题、上下文省略、章节区间/列表或疾病主题提出计划时，系统都经过同一个语义解析合同，落到真实可执行范围；无需记忆数据库中的精确名称。
- Current observed problem or limitation: 当前 parse contract 以单一 `chapter_key`/`book_title` 为中心，无法表达章节集合，并容易把不精确标题判为无范围；继续增加类型/措辞分支会造成不可维护的穷举。
- Scope: 所有学习计划首次请求和正式计划澄清续答中的 scope 识别；目标实体类型至少覆盖 book、chapter、knowledge_point、disease、weakness_profile，并支持一个或多个有序目标。
- Preserve: MasterRouter 只负责高层 intent/handoff；真实 KG、文档、画像/掌握度仍是事实真源；确定性代码只做候选检索、ID 校验、集合运算、置信/歧义门槛、权限与可执行性验证。首次计划排程和后续 LLM 修改边界不变。
- Replace/remove: 替换按书/章节/知识点/疾病分别解析及原始文本关键词分支；旧 `chapter_key`/`book_title` 可作为兼容输入，但正式内部结果必须归一为统一有序 scope selector/resolution。
- Compatibility/migration: 保持现有 API 和旧草案读取；解析结果可在 `study_plan.py` 内部适配旧字段，不要求历史迁移。不得新增平行 Router 或第二套计划 API。
- Acceptance evidence: 对每种实体至少两种与正式名称不完全相同的自然表达，加跨回合省略、前/后 N、区间、离散列表、标题+序号混合；证明统一 resolver 输出真实 ID/provenance/order，唯一高置信候选直接生成，真正多候选才具体澄清，零候选不猜测；禁止硬编码样例。最终在真实前端完成生成→LLM 修改→启动。
- Unknowns requiring user decision: 无。模糊匹配允许 LLM 在真实候选集合上做语义选择；不得让 LLM创造不存在的实体。若候选并列且无法可靠区分，系统应列出具体候选澄清，而不是随便选一个。

### 2026-07-16 统一解析首轮代理实现失败

- 首轮实现新增了 `parse_scope_selector_from_text`、selector validator/resolver 及测试，但生产代码中除函数定义外没有调用；`parse_plan_request_from_text` 和正式推荐链路仍使用旧 `chapter_key/book_title` parser。14 项 selector 聚焦测试和 20 项真实 KG 旧范围测试通过，只证明旁路函数可工作，不能证明真实请求经过统一解析。
- 判定为 proxy failure：下游形似目标合同，但未接入真实生产 consumer。下一任务必须把正式 plan parse/handoff 切换到一次统一 LLM selector，并携带有序 `resolved_scopes` 进入现有范围解析；禁止保留旧 parser 为静默正式 fallback。旧字段只能作为同一结果的兼容投影，不得触发第二套语义判断。

### 2026-07-16 生产计划项资源预检独立通过与健康审计节点

- 生产事实：四个草案持久化入口（结构化自定义、章节首次确定性、薄弱点首次确定性、LLM 编排）均在 `store.save()` 前调用私有 `_preflight_draft_items(items)`，未传关闭 KG/evidence 检查的参数；默认 inspector 强制调用真实 `preflight_scope_resources(..., check_kg_nodes=True, check_evidence_chunks=True)`。
- 旧路径移除：`checks_performed` 为空不再视为可用；生产入口不再以 `check_kg_nodes=False` / `check_evidence_chunks=False` 跳过资源检查。测试隔离只允许向私有 helper 注入 callable，不能由请求、公开 API 或计划字段控制。
- 独立证据：资源合同聚焦 28 passed、真实 KG 范围/计划/薄弱点 15 passed、计划/API/正式范围合同 484 passed，`git diff --check` 通过。该证据只验证“不可执行草案不得保存”，不等于真实前端已能正式化并启动所有任务。
- 后续最小节点：在继续真实页面矩阵前执行一次只读项目健康审计，逐段核对 `真实计划请求/草案 -> 正式计划项 -> learn/review/basic_practice 启动 -> scoped lesson/stage exam` 的生产 handoff、既有测试和当前 cannot-prove。审计不得改代码；若发现断链，下一任务只修最小断点，禁止同时扩张前端或阶段卷内部实现。

### 2026-07-16 健康审计发现：正式项 session 回绑与基础练习依赖断链

- 审计通过部分：草案生成/preflight、正式项 `learning_scope` roundtrip、learn/review scoped lesson 路由、basic/advanced stage-exam consumer、session 完成与计划项完成分离均有独立自动证据。
- 确认断点：学习计划页把 draft scope 的 `document_id` 放入 `linked_course_id`，但 Router 的 `basic_practice` 明确把 `linked_course_id` 当作真实学习 session ID；教材文档 ID 不是 session ID。与此同时，正式 `learn_chapter` / `review_chapter` 启动后创建的新 session 没有回绑到该正式计划项及其同 scope 练习依赖，因此随后基础练习可能稳定报 session 不存在或缺少关联课程。
- 通用规则：正式学习/复习任务启动成功后，必须把真实 session ID 记录到该正式项；基础练习必须通过显式同 scope 前序正式项解析真实 session，再调用既有 `ensure_stage_exam_for_interaction`。不得把 document/chapter/KG ID 解释成 session，不得为练习静默启动普通学习，也不得修改阶段卷内部实现。
- 最小修复边界：复用现有 `dependency`、`session_id`、`linked_course_id` 字段和正式计划项 update 能力，补齐 API/正式化兼容输入与 Router 回绑/解析；不新增表、不新增执行器、不改学习计划页面交互或其他前端页面。
- 验收：两个不同 scope 的正式链路都必须证明 learn/review 启动得到 session 并回绑，basic_practice 只取得其同 scope dependency session；document_id 伪 session、跨 scope dependency、未先启动依赖均必须 fail-closed。通过后仍需真实页面按顺序实际启动。

### 2026-07-16 session 回绑首轮代理成功：真实草案采纳 consumer 未接入

- 已实现但未形成生产闭环：后端 API/schema 已能承接 `dependency`、`practice_level`、`session_id`；Router 已能在正式 learn/review 成功后回绑真实 session，并让 basic_practice 通过正式 dependency 解析 session。相关聚焦测试通过。
- 生产反证：真实 `frontend/js/learning-plan.js` 的 draft 映射/采纳 payload 仍不传 `dependency`、`practice_level`、`session_id` 或 `day_offset`；它把 `learning_scope.document_id` 直接写入 `linked_course_id`，并把所有 draft items 以当前选中日期提交。后端新增字段只被测试直接构造，真实 consumer 不会产生它们。
- 判定：proxy failure。不得把“API 接受字段 + Router fixture 通过”记为正式执行闭环。旧生产路径仍会丢依赖、把多日计划压到一天并用 document ID 冒充 session。
- 架构纠正：只补已有学习计划页的内部数据 handoff，不改变页面布局、按钮、文案或用户交互模型。draft item 的 `day_offset`、dependency index、practice_level 必须保留；采纳时按 base date + offset 保存，并把 dependency index 转换为已采纳前序正式 item ID；`linked_course_id` 只能传真实 session，绝不能由 document_id 填充。
- 个别采纳边界：若用户单独采纳有 dependency 的练习项，必须先按同一 draft 依赖链采纳前序项，或明确拒绝；不得保存孤立练习。批量采纳必须按 draft 顺序建立正式 ID 映射。

### 2026-07-16 草案采纳 handoff 首轮实现仍需严格收口

- 已实现：真实 `learning-plan.js` 已保留 `day_offset`、dependency、practice_level、session_id；日期按 base + offset 计算，document_id 不再写入 linked_course_id，批量路径维护 draft index 到正式 item ID 的映射。
- 独立失败：dependency 为非法字符串、缺失索引或循环时，当前 helper 会静默忽略或提前标记 visited，随后仍可能 POST 不带 dependency 的练习项；这违反“不得保存孤立练习”的 fail-closed 门禁。
- 证据缺口：新增 Node 测试只调用映射/payload/排序纯 helper，没有实际执行 `acceptRecommendation` / `acceptAllRecommendations` 并捕获真实 POST 顺序和响应 ID 映射，仍可能在正式 consumer 中断链而测试通过。
- 最小返工：依赖图先做完整校验（整数、存在、只引用前序、无自依赖/循环），任何失败在第一个 POST 前停止；单条递归和批量采纳复用同一校验/采纳 helper。Node harness 必须注入真实 `apiPost` spy 并执行生产采纳函数，验证 POST 日期、顺序、正式 dependency ID 和零孤立写入。

### 2026-07-16 草案采纳与正式执行后端闭环进入真实页面验收

- 自动验证事实：真实 `learning-plan.js` 生产函数已完成完整 dependency 图 fail-closed、按 day_offset 保存多日日期、API 返回 formal ID 映射、单条递归前序采纳、批量拓扑采纳，以及 document_id/session ID 语义分离。Node 真实 POST spy 57 passed；计划/API/正式合同 495 passed；Router 42 passed；真实 KG 15 passed。
- 当前证据边界：这些证据证明代码 handoff，但尚未证明真实浏览器页面、真实 provider、真实持久化数据库和实际 lesson/stage-exam consumer 在同一流程中成功。
- 下一验收节点：先用一个模糊单章自然表达完成最小真实链：聊天首次生成 -> 同会话真实 LLM 修改 -> 打开计划页并采纳 -> API/UI 证明多日和 dependency -> 在真实 Master Chat 依次启动 learn、review、basic_practice。使用隔离用户与服务端口，保存页面截图和运行时 IDs，清理隔离数据库记录/服务进程。
- 通过条件：初版范围正确且为三项/两日；修改生成新 draft 并保留 scope、满足新时间/重点要求；采纳后正式项日期/依赖正确；learn/review 返回真实 session，basic_practice 使用 dependency session 显示阶段卷；无 `scope_no_resources`、document ID session、全局 fallback、空内容或错误路由。

### 2026-07-16 R10 首个模糊单章真实页面验收失败

- 原节点/假设：自动 handoff 通过后，可直接在一个真实浏览器任务中完成模糊单章首次生成、LLM 修改、采纳和三类任务启动。
- 独立反证：`task-20260716-195912` 已终态、exit code 3；Codex 打开真实页面截图确认首次输入后显示“学习计划推荐失败：LLM returned empty or non-string output”，没有 draft 或计划卡，后续修改、采纳和启动均未到达。隔离端口 8001 已停止，`docs/project-notes.md` 不存在，真实 KG 聚焦回归 15 passed、`git diff --check` 通过。
- 证据冲突：runner 的追加诊断声称 `parent_book_title` 书名号导致精确字典查找失败，并给出正确 chapter scope ID；但页面正式错误发生在 selector JSON 解析前的空输出，且运行脚本丢弃了服务 stdout/stderr。该根因不能作为独立验收事实，只能视为待复现假设。
- 判定：证据失败并伴随潜在实现失败。R10 第 1 个场景仍 failed/cannot-prove；“11 场景最终矩阵”尚未开始累计通过数。
- 精确变更：把下一节点缩小为“首次模糊单章生成断点诊断与修复”。隔离服务必须保存脱敏的 provider 响应形态/finish reason/重试分类，证明第一失败边界；只修该边界。若真实输出已是合法 selector 但书名格式未规范化，则在统一 resolver 中按真实候选归一化匹配；若 provider 返回空/非标准内容，则在 LLM adapter 边界做有界、同 prompt、同模型兼容处理。两种情况都禁止按牙周病学、第三章、输入句或关键词写专用分支，禁止模板/猜测 fallback。
- 本节点验收：同一模糊输入在真实页面产生唯一真实章节的 3 项/2 日首次计划；再用不同教材或不同书名格式的结构化样例证明通用性；保留非法/歧义 fail-closed。只在首次生成通过并展示真实截图后，才恢复原完整链任务的修改、采纳和三类启动部分。

### 2026-07-16 模糊解析已到达真实章节，KG 标识空间断链阻止计划卡

- 独立审查结论：`task-20260716-202838` 修复后，真实 provider selector 已把模糊输入解析到唯一真实章节 `chapter:010c459fd2410a8a`；聚焦自动回归 14 + 15 passed。但真实页面仍显示 deterministic chapter plan preflight failed，没有 draft 或计划卡，因此任务目标未完成，判定需要返工。
- 对 runner 根因的纠正：`kg_nodes.jsonl` 并非“没有 node_id”；Codex 实读确认节点存在 `node_id=kg://...`。真正断点是两种合法标识空间未映射：章节 overview/scope 使用稳定语义 ID `core:<digest>`，原始 KG 节点和 edge/question loader 使用物理 ID `kg://<book>/<chapter>/<client_ref>`。preflight 和 `plan_lesson` 都把前者直接与后者比较，必然得到 0；这也是现有测试用 raw `node_id` 伪装 scope 能通过而真实章节 scope 失败的原因。
- 架构纠正：在 learning planner 内建立唯一、确定性的 scope-core → raw-node 映射。映射必须复用知识图谱 producer 的稳定 core identity 规则，并用 document/chapter 约束消除同名核心跨章重复；preflight 返回请求中确实可用的稳定 `core:*`，实际 lesson consumer 使用对应的 raw `kg://` 节点加载 edges/questions。不得改变正式 scope ID、不得把所有同名跨章节点混入、不得按本章节硬编码。
- 兼容边界：既有直接携带 raw `kg://` ID 的内部调用继续可用；不存在的 stable/raw ID、document/chapter 不一致和跨章命中必须 fail-closed。不修改知识图谱展示 API、题库、阶段卷、前端或计划排程。
- 验收：两个不同教材的真实章节分别证明 stable IDs 能映射到本章 raw nodes；同名跨章节点只取当前章；preflight 与 `plan_lesson` 使用同一映射结果。随后真实页面原模糊请求必须出现唯一章节 3 项/2 日计划卡和 draft ID，才算该首次生成节点 verified。

### 2026-07-16 stable-core 映射首轮为代理通过，必须架构收口

- 独立判定：`task-20260716-210318` 自动测试通过，但未执行真实页面验收，artifact 只有 JSON、没有截图或 draft ID；隔离 uvicorn 还遗留在 8002，Codex 已确认命令行后停止。任务目标未完成，判定 implementation + evidence failure。
- 违反架构边界：planner 复制了 `hashlib.sha1(book_title\0node_type\0canonical_name)`，没有复用 KG producer 的真实函数；这会在 producer 规则变化时再次漂移。
- 负例为伪通过：raw `kg://` 测试明确允许 document/chapter 不匹配仍通过，与任务要求的 scope mismatch fail-closed 相反；跨章测试传空 ID，helper 直接 fast-path 返回空，因此没有验证任何跨章去重；chapter mismatch 只断言“比无约束少”而非空。
- 正式修正：知识图谱内部仅公开一个无副作用 stable core identity helper，原 `_core_id` 保持兼容别名；planner 直接调用该 helper，不得自己散列。stable 与 raw ID 都必须经过 document/chapter/book 约束；不存在或不匹配返回空。移除未知格式 direct fallback。
- 验收边界：真实非空 stable ID 在同一教材多个章节有 raw 节点时只返回指定章；raw ID 在 scope mismatch 时拒绝；两个教材通过；preflight 与 plan_lesson 共用映射。必须完成真实 Playwright 页面、计划卡、draft ID、3 项/2 日截图并清理端口，才可推进后续。

### 2026-07-16 stable-core 架构收口自动验证通过，真实页面证据仍缺失

- 独立代码结论：`stable_core_identity` 已成为 KG producer 的公开纯函数，`_core_id` 保持兼容；planner 已删除复制 SHA1，并严格限制 stable/raw ID 的 document/chapter/book scope。跨章测试使用非空 ID，raw/chapter/book mismatch 与未知格式均严格为空。
- 独立自动证据：MasterRouter/StableCore/preflight/plan_lesson 22 passed；真实 KG scope/plan 15 passed；`git diff --check` 通过。正式命令 `test_study_plan_memory.py -k "chapter_sequence or preflight"` 选中 0 项，不能记作通过，但不否定上述实际选中测试。
- 任务未完成项：任务没有创建指定 artifact 目录，没有 Playwright 截图、draft ID 或页面正反 marker。因此架构实现可记为 implemented/自动 verified，但用户可见首次生成仍 cannot-prove；按可视验收门禁判定需要 evidence rework。
- 下一最小节点：只做隔离真实浏览器首次生成验收，不允许改任何产品代码或测试。原模糊输入必须产生唯一章节、3 项/2 日、draft ID和“打开学习计划”；失败则保存第一断点、服务日志和截图。通过并向用户展示截图后再恢复修改/采纳/启动链。

### 2026-07-16 最终浏览器验收责任边界

- 用户明确决定：runner/Claude 可以先用 Playwright 做预检、定位和生成工件，但任何真实前端场景最终是否通过，必须以 Codex 自己再次进行的真实浏览器操作结果为准。
- 证据等级调整：runner 页面截图、DOM断言、draft/API记录只能把场景推进到“待 Codex 浏览器复验”；不得单独升级为 verified。Codex 必须亲自打开真实页面、输入自然语言、观察终态、点击必要入口，并核对运行时 draft/formal/session 数据。
- 通过条件：Codex 浏览器操作与结构化数据一致，且页面不存在禁止 marker；runner 成功但 Codex 复验失败时按真实失败返工。Codex 无法完成浏览器操作时状态为 cannot-prove，不得用 runner 结果替代。
- 适用范围：R10 浏览器场景、同会话修改、采纳和 learn/review/basic 启动；最终是否通过均以 Codex 自己操作为准。场景数量随后由 2026-07-17 用户授权精简为 5 条初版、3 条修改和 1 条三类型启动链。

### 2026-07-16 真实页面预检发现规范书名身份漂移

- 原节点/假设：KG producer 的 `stable_core_identity` 已公开并被 planner 复用后，真实 overview `core:*` 应能在同 document/chapter/book 约束下映射到 raw `kg://` 节点。
- 独立反证：`task-20260716-214342` 的真实页面预检正确解析到 `document.d766e8629232a2f6` / `chapter:010c459fd2410a8a`，并生成 3 个确定性任务，但保存前 preflight 对 3 项全部报无可用 KG 节点。Codex 独立查看页面截图和 evidence，并实读真实节点：overview 使用规范短书名 `牙周病学` 生成稳定 ID；raw 节点 `book_title` 为 `牙周病学（孟焕新主编，第5版，人民卫生出版社）`，而 `subject` 为 `牙周病学`。以 raw 长标题计算的 stable ID 与 overview 完全不相交，以同一规范短标题计算则命中。
- 失败分类：implementation failure。散列函数虽已单一真源，但散列输入的规范书名仍有两个来源；planner 直接使用 raw `book_title`，没有复用 KG producer 在 overview 建模时使用的 `_book_style(document_id, raw_title)["title"]` 规范身份。
- 通用规则：稳定 core 身份必须由 KG producer 同时拥有“规范书名输入”和散列规则。raw 节点映射前应通过 producer 暴露的无副作用规范书名 helper，按 document_id + raw title 得到与 overview 一致的 canonical title，再校验它与 scope book 相符；不得在 planner 里去括号、截断、模糊包含或按教材写分支，也不得在 stable ID 失败后退化为仅名称匹配。
- 最小架构修正：只公开/复用 KG producer 的 canonical book-title identity helper，并让 planner 的 stable/raw scope 映射使用该结果。既有 stable IDs、overview/API、正式 scope、raw `kg://` 兼容路径均保持不变；错误 document/book/chapter 继续 fail-closed。
- 验收：当前牙周病学真实长标题样例、另一种标题格式的第二教材、错误 document/book 负例和 raw ID 严格范围均通过；preflight 与 `plan_lesson` 共用映射。runner 浏览器只作预检，最终首次生成仍须 Codex 自己真实浏览器操作后才能升级 verified。

### 2026-07-16 规范书名局部转换实现通过，转入最小页面预检

- 独立代码结论：KG producer 仅新增无副作用 `canonical_book_title(document_id, raw_title)`，现有 overview 输出、stable ID、raw KG 数据和 API 均未改变；planner 只在自己的 stable/raw mapping 中调用该 helper。未新增模糊包含、名称 fallback、索引重建或其他消费者迁移。
- 兼容回归：Codex 独立运行 producer/mapping 18 passed、真实 KG 37 passed、StableCore/preflight/plan_lesson 40 passed，以及 MasterRouter、计划 memory、正式 scope、API、真实 KG 合计 668 passed；`git diff --check` 通过。错误 document/chapter/book 和 raw ID scope mismatch 继续 fail-closed。
- 证据缺口：runner 因 SessionEnd hook cancelled 终态 exit code 3，未创建指定真实页面 artifact；因此实现和自动回归可以通过，但真实前端仍 cannot-prove。
- 下一最小节点：禁止修改任何产品代码和测试，只用隔离服务、唯一用户和真实页面重跑原模糊输入；确认出现唯一第三章、3 项/2 日、非空 draft ID，且不出现 preflight/scope/LLM 错误。顺带执行一个普通非计划问答/学习入口的轻量 smoke，证明局部转换未破坏正常使用。runner 结果仍只作预检，最终验收由 Codex 自己浏览器完成。

### 2026-07-16 模糊单章首次生成 runner 预检通过，恢复完整链预检

- runner 诊断证据：真实页面原模糊输入生成 `draft_86cabc59ac4d9a5df2eae728c279132c`，唯一 scope 为 `牙周病学 / 第三章 牙周病的分类和流行病学 / document.d766e8629232a2f6`；3 项为 learn、review、basic practice，day offsets `[0,0,1]`，页面有“打开学习计划”，无处理失败、LLM 空输出、scope 或 preflight 错误。
- Codex 独立工件审查：截图与 `evidence.json` 的可见标题、任务数、分钟和结构一致；隔离端口已停止。该证据只把首次生成推进到“runner 预检通过、等待 Codex 浏览器复验”，不能升级 R10 verified。
- 普通入口边界：非计划请求页面只显示“已进入总控队列/正在识别需求”，没有等待最终回答；因此仅证明提交入口未崩溃，不记为普通问答功能通过，也不据此新增修复逻辑。
- 下一最小节点：不改产品代码，恢复同一模糊单章场景的 runner 完整链预检：首次生成 -> 同会话真实 LLM 修改 -> 打开计划页并采纳 -> 核对正式项日期/依赖 -> 依次启动 learn/review/basic practice。失败只记录第一断点；成功后仍由 Codex 自己浏览器复验。
- 用户补充的执行门禁：点击“开始计划”后，session ID、202 响应、后台队列或加载状态均不能算通过。必须等待真实页面显示非空、可读、与选中章节 scope 一致的学习/复习内容；basic practice 必须等待真实题目内容和可作答入口出现。只有这些最终内容正常生成且页面无错误，任务启动才可记为预检通过。

### 2026-07-16 完整链第一断点：LLM 修改生成非法练习依赖

- runner 真实页面证据：首次模糊单章继续成功；同会话输入“每天最多30分钟、减少练习量、范围不变”后，真实 LLM 返回 5 项，但其中 item 4 `basic_practice` 依赖 item 2 `basic_practice`。严格 preflight 正确拒绝并在页面显示错误，新 draft 未保存，采纳与启动阶段未到达。
- 根因边界：基础 orchestration prompt 只约束 dependency 必须引用更早 item，没有说明 basic/advanced practice 必须依赖同 scope 的 learn/review；候选 schema validator也只检查索引先后，把任务类型语义留到持久化前 preflight。preflight 失败发生在现有 LLM retry loop 之后，因此没有机会让 LLM按验证错误修正。
- 最小修复：在统一 orchestration contract 中补充通用依赖语义，并让候选 validator 在持久化前识别 practice→practice、无依赖和跨 scope 依赖。对该类可纠正的 LLM 输出最多做一次带明确验证反馈的真实 LLM 重试；第二次仍非法则保持 fail-closed。确定性代码不得重新排列、改依赖或生成替代计划。
- 保持边界：不改首次确定性排程、KG/KAG、资源 preflight、前端、正式化、Router、session 或阶段卷；不按牙周病学或本次措辞写分支。
- 验收：受控首轮非法/次轮合法证明真实重试，连续非法证明无模板兜底；真实页面修改生成新 parent-linked draft并满足30分钟/减少练习/scope保留。随后继续原完整链，直至页面真实学习正文和题目，或保存下一第一断点。

### 2026-07-16 依赖合同实现自动回归通过，集成与页面证据缺失

- 独立实现审查：统一 prompt 已明确 practice dependency；candidate validator 对 null、practice→practice、跨 scope 做稳定 `practice_dependency_invalid`；现有 retry loop在该错误且有预算时用具体错误构造反馈 prompt，不改写 candidate。合法输出仍沿原路径。
- 独立回归：聚焦命令实际选中 4 passed；完整 Python 回归 675 passed；`git diff --check` 通过。`tests/test_real_kg_runtime_acceptance.py -k "orchestration and dependency"` 选中 0 项，不能作为该模块证据。
- 任务缺失：没有测试 `create_plan_draft_from_text` 的真实两次 `_call` 序列、parent draft保存和连续非法不保存；runner 报告也明确承认“连续两轮非法由结构保证”而未执行。指定真实页面完整链未重跑，artifact 只有实现摘要/断点，没有截图或运行时 draft/formal/session/exam。
- 判定：evidence failure。下一最小节点禁止改产品代码，只补 orchestration服务级集成测试并重跑真实完整链。若测试暴露实现错误，再提交代码返工；不得先假定通过。

### 2026-07-16 依赖重试服务级集成独立通过，立即执行纯页面完整链

- Codex 独立验证 `OrchestrationDependencyIntegrationProofTests` 3 passed：首轮非法/次轮合法恰好两次 orchestration并保存新parent draft；连续两轮非法抛稳定错误且零新draft；合法首轮恰好一次。完整计划/Router/API/真实KG回归 678 passed，diff check通过。
- 边界：新增证据只修改测试；产品实现未再变化。该服务级行为可记自动 verified，但 runner 没有按正式任务执行真实页面完整链，未产生任何首次/修改/采纳/学习正文/题目截图。
- 判定：evidence failure。下一任务为纯页面运行，不允许代码或测试写入，也不再重复自动测试。必须直接用隔离真实服务、真实provider和页面走完整链，直到真实学习正文和题目或下一第一断点。

### 2026-07-16 浏览器验证职责最终调整

- 用户明确要求：鉴于 runner 多次未完成完整页面链，当前已提交任务结束后，不再向 runner 发布任何浏览器或页面验收任务。
- 新职责边界：runner 后续只承担代码修改与自动测试；所有真实页面生成、LLM修改、采纳、正式计划启动、学习正文和题目验收均由 Codex 自己操作浏览器。
- 证据规则：当前 runner 页面任务的任何结果仅作诊断，不用于升级场景状态；后续不存在“runner页面预检通过”作为必经节点。Codex浏览器失败直接成为真实返工证据。

### 2026-07-16 完整链诊断后的最小返工：修改意图必须可验证

- 上一页面任务终态证据：首次模糊单章生成、parent-linked 修改、7 个正式项采纳、复习正文和基础练习 4 道真实题目均出现；首个 learn 页面在约 129 秒时仍显示后台队列，但其依赖 basic practice 后续成功，说明该 learn 工作至少在后台产出了可用 session，现有证据不足以把它定性为正式执行链代码断裂。
- 明确失败：用户要求“每天最多 30 分钟、减少练习、范围不变”，新草案虽保持 scope 和单日预算，却从原 3 项扩张为页面采纳 7 项，其中 3 个 basic practice，不能证明“减少练习”被执行。
- 变更解释：统一 LLM selector 负责把修改语义解析为类型化 revision constraints；确定性 validator 只比较 parent/new 的 scope、每日分钟、练习项数量等可测量事实。不得在 Python 中枚举中文关键词，不得让 validator 改写候选或生成模板计划。
- 架构边界：只扩展 `study_plan` 内已有 selector → orchestration context → candidate validator 的本地合同和聚焦测试；不改 KG/KAG、planner、Router job、正式项、前端、session、Tutor 或阶段卷。页面最终验收仍由 Codex 自己完成并等待真实正文/题目。
- 当前判定：R10 首场仍 failed/cannot-prove；下一正式任务为修改约束合同实现与自动测试，runner 不再执行浏览器。

### 2026-07-16 Codex 最终浏览器验收等待策略修正

- 用户确认的运行事实：既有下游真实学习内容生成链本身较慢，129 秒大概率仍得不到最终正文，属于正常时延，不能把这一时点的后台队列状态判为产品失败。
- 解释边界：不修改前端超时、Router job、Tutor、KAG 或下游生成链；仅延长 Codex 自己的最终验收观察窗口。此前“不得延长前端等待掩盖 provider 阻塞”的限制继续有效，本变更不是产品代码超时放宽。
- 验收规则：Codex 点击正式计划启动后，至少允许一个完整慢链窗口（默认 10 分钟）；期间同时轮询 router job 是否仍 running/产生 progress、正式项是否回绑真实 session、页面是否出现最终正文/题目。只要 job 有推进且无 terminal error 就继续等待；仅在 job 明确 failed、长时间无任何事件/状态变化且超过窗口，或页面最终内容错误时判失败。
- 完成门槛不变：后台队列、session ID、202 或加载态仍不算通过；必须最终看到 scope 一致的真实学习/复习内容和真实可答题目。

### 2026-07-17 修改约束实现通过回归，但两个证据样例需补齐

- 独立判定：`需要返工`（evidence failure）。runner 因 SessionEnd hook cancelled 记录 exit code 1；Codex 独立运行修改/依赖聚焦 11 passed、完整计划/Router/API/正式 scope/真实 KG 回归 685 passed、`git diff --check` 通过，未发现其他链路回归。
- 实现审查：可选 `revision_constraints`、parent metrics、revision validator 和共享一次重试均已进入现有 `study_plan` 路径；无 Python 中文关键词分支、无 candidate 改写、无前端/Router/KG/阶段卷改动。
- 证据缺口一：“scope maintain violation”现有测试使用虚构 scope ID，先由标准 `invalid_scope_ref` 拒绝，没有证明两个合法 parent scopes 被候选漏掉一个时会触发 `revision_scope_not_maintained` 和有界重试。
- 证据缺口二：要求的结构不同组合是“增加复习、练习保持、每日 20 分钟”；现有测试实际覆盖“增加学习、练习保持”，且未组合每日 20 分钟。
- 下一最小任务：禁止改产品代码，只在 `test_study_plan_memory.py` 通过真实 `create_plan_draft_from_text` 服务入口补齐上述两个样例并复跑全回归。

### 2026-07-17 修改约束自动合同独立通过

- 独立判定：`通过`。证据返工任务虽 runner exit code 0/完成标记可读，Codex 仍独立复跑。
- 证据：`RevisionConstraintIntegrationTests` 与 dependency 集成共 10 passed；计划、Router、API、正式 scope、真实 KG 完整回归 685 passed；`git diff --check` 通过；`docs/project-notes.md` 不存在。
- 两个缺口已真实覆盖：两个合法 parent scopes 中首轮漏一个触发 `revision_scope_not_maintained`、第二轮全 scope 保存；“增加复习+练习保持+每日20分钟”一次 orchestration 保存且逐日预算满足。
- 状态：修改约束的自动合同升级为 verified；真实 provider 是否稳定解析这些 revision constraints、页面是否正确显示修改结果仍由 Codex 浏览器最终验证。
- 下一节点：发布最后一个 R9 后端入口矩阵测试，仅验证整本书、真实薄弱点和澄清续答的生产形态服务/Router handoff；不再修改当前 revision 实现。

### 2026-07-17 R9 后端入口矩阵独立通过

- 独立判定：`通过`。Codex 运行 whole-book/weakness/clarification/continuation/revision/plan-item 聚焦 105 passed；计划、Router、API、正式 scope、真实 KG 完整回归 696 passed；diff check通过。
- 证据：两个结构不同教材满足 N章→N+1天、3N项和每章 learn/review/basic；两类 learner memory/mastery 弱点证据满足 N弱点→N天、2N项 review+basic，无证据诚实返回；书名短续答和章节短续答经 clarification parent 进入 study_plan，普通 learning、Direct QA、显式计划项保持。
- 状态：R9 后端自动合同升级为 verified；真实 provider 语义稳定性和页面显示仍 cannot-prove。
- 下一节点：runner 只执行一次真实 provider 的非浏览器语义冒烟，验证统一 selector 对模糊书名、修改约束和澄清短续答的真实输出；不修改产品代码、不启动学习内容慢链。随后由 Codex 自己进行最终浏览器验收。

### 2026-07-17 真实 provider 修改约束冒烟取证返工

- 原节点/假设：`real-provider-semantic-smoke-1` 将 Scenario C 的 `revision_constraints=null` 解释为真实 provider 未输出类型化修改约束。
- 独立反证：正式持久化合同位于 `payload.request.scope_selector.revision_constraints`，但冒烟脚本读取的是 `payload.request.revision_constraints`。现有 JSON 因读取路径错误，既不能证明约束存在，也不能证明约束缺失；Scenario C 虽结果恰好满足练习减少、范围保持和每日 30 分钟，也不能替代 selector 合同证据。
- 失败分类：evidence failure。不得在该证据基础上修改产品解析器、编排器或下游链路。
- 最小修正：只修非浏览器真实 provider 冒烟脚本的合同读取与断言，重新运行父草案→修改子草案场景；记录嵌套 selector 原始字段、parent/new 可测量指标以及约束校验结论。若真实字段仍缺失，任务必须非零退出并保留脱敏证据，后续才发布局部产品返工。
- 保持边界：不改 `study_plan.py`、KAG/KG、Router、前端、Tutor、阶段卷、正式计划或慢内容生成链；不新增关键词逻辑、临时 endpoint 或并行计划链。
- 证据等级：本节点最多将“真实 provider 是否输出修改约束”推进到 verified 或 failed；真实页面生成、修改、采纳和内容启动继续只能由 Codex 自己浏览器验收。

### 2026-07-17 修改约束真实值已证明，证据 schema 仍需严格收口

- 独立结果：真实 provider 已在正式嵌套路径输出 `scope_relation=maintain`、`practice_count_relation=decrease`、`daily_minutes_max=30`；子计划 scope 相等、练习数 1→0、最大单日 30 分钟，新计划续答没有 revision constraints。产品行为没有暴露失败。
- 剩余证据故障：正式任务要求字段名 `revision_constraints_path`、场景状态 `passed`，脚本却写成 `revision_constraints_read_path` 和 `ok`；因此正式独立断言报 `KeyError`，不能按合同记为通过。
- 失败分类：evidence failure，不是产品实现失败。不得因此修改 selector、validator、KAG/KG、Router、前端或下游内容链。
- 最小修正：仅统一证据字段名与 pass/fail 状态语义，确保任一合同断言失败时状态为 `failed` 且非零退出；重新生成同一真实 provider 脱敏 evidence，并让正式独立断言原样通过。

### 2026-07-17 真实 provider 非浏览器语义证据通过，进入最终健康核验

- 独立判定：真实 Provider 修改约束证据通过。Scenario C 的正式嵌套路径、三个精确约束、scope 保持、练习 1→0、每日 30 分钟和 `status=passed` 均由 Codex 原样断言通过；Scenario D 新计划续答未误带 revision constraints。
- 兼容边界：本轮只改一次性冒烟脚本与 evidence，产品文件时间戳和既有 dirty worktree 未因任务变化；无 commit、`docs/project-notes.md` 不存在，`git diff --check` 通过。
- 状态推进：真实 provider 的模糊章节、整书、修改约束、澄清书名续答四类非浏览器语义冒烟升级为 verified。真实前端、采纳、启动和最终内容仍不得由该证据替代。
- 下一节点：在最终 Codex 浏览器验收前执行一次只读 project-health-verification，汇总正式生产 handoff、禁止路径、未证明项和跨链风险；不得修改文件、运行会写缓存/数据库的测试或替代浏览器验收。

### 2026-07-17 最终健康核验通过，先做真实 API 完整链就绪验证

- 独立判定：只读健康核验通过。Codex 复查引用确认统一 selector、缺 scope fail-closed、practice→stage exam、显式计划项和普通学习/Direct QA 保持链均存在；无 P0/P1、无新增 commit、`docs/project-notes.md` 不存在、真实 Provider evidence 仍有效。
- P2 边界：`_build_draft_items` 仅由旧 `create_plan_draft` 使用，`_build_scope_candidates_for_orchestration` 只有定义、正式文本入口显式不调用。依用户“尽快闭环、不要叠加无关逻辑”，当前不删除、不迁移、不扩大清理范围。
- 浏览器边界：最终验收仍必须由 Codex 自己真实浏览器操作；runner 不得执行页面。当前会话未暴露 Browser skill 所需的正式浏览器控制调用，禁止改用 standalone Playwright 或临时页面链冒充。
- 最小继续节点：runner 仅执行一个隔离真实 API 完整链就绪验证，使用真实 Provider、正式计划 API 和既有学习/阶段卷入口完成单章首次生成→修改→采纳→启动，并按用户确认的慢链窗口等待真实学习/复习正文和基础练习题目。该结果只证明后端/运行时就绪，不升级页面 verified。

### 2026-07-17 真实 API 完整链首轮为代理证据，重写验证边界

- 独立判定：`需要返工`（proxy + evidence failure）。任务通过直接导入 `create_plan_draft_from_text` 完成初版和修改，违反“全程正式 HTTP、不得内部函数绕过”；只能证明 study_plan 服务本身，不能证明 MasterRouter/正式聊天入口。
- 任务设计冲突：单章父计划只有一次 basic practice，“减少练习”正确执行后子计划为零练习，因此不可能同时要求“采纳修改子计划并启动 basic practice”。首轮脚本仍把仅 learn/review 的 2/2 激活标为完整成功，且正式验收所需 `status=passed`、`initial_launch.basic_practice` 等字段不存在。
- 已获得但不升级的诊断：初版 3 项生成；修改 maintain/decrease/30；learn/review 经正式计划项启动分别生成 819/787 字正文和问题。它们是部分真实运行证据，不足以通过完整任务。
- 最小修正：
  1. 初版和修改均通过 `/api/router/jobs`，禁止导入计划生成函数。
  2. 先采纳初版并启动 learn/review/basic，证明三类既有链路和真实题目。
  3. 再通过 parent draft 的正式 HTTP 修改，采纳子计划并启动其实际保留的全部类型；“减少到零”时不虚构 basic，但至少验证修改后 learn/review 真实内容。
  4. 任一服务中断、缺题、部分激活或 HTTP 绕过均为 failed，不能写 complete。
- 产品边界不变：不修改 Router、study_plan、前端、KAG/KG、Tutor 或阶段卷；本节点只修隔离运行脚本和证据。

### 2026-07-17 全 HTTP 脚本已重写但未执行，必须证据收口

- 独立判定：`需要返工`（evidence failure）。`run_readiness.py` 已删除 `backend.services.*` 业务导入，并实现初版/修改 `/api/router/jobs`、初版三类启动和独立 evidence 结构；但 runner 没有执行正式运行命令。
- 反证：`evidence.json` 与 `run.log` 的时间和内容仍来自旧的直接导入版本，继续显示 Phase 1/2 direct import 和仅 2/2 激活；不能作为新脚本证据。
- 最小修正：必须实际运行新脚本，允许仅在同一脚本内修复运行时取证/HTTP payload/轮询错误并从头重跑，直至正式独立断言通过或保存不可绕过的第一产品断点。不得再次以“脚本已写好”结束。
- 保持边界：不改任何产品、测试或页面；最终浏览器仍由 Codex 自己完成。

### 2026-07-17 全正式 HTTP 真实链已通过，剩余顶层 evidence 投影

- 独立运行事实：正式 `/api/router/jobs` 已完成初版生成；初版采纳后 learn/review/basic 均产生真实内容，其中基础练习有 5 道可答题；parent-linked 修改产生 maintain/decrease/30，练习 1→0、scope 保持；修改子计划 learn/review 均产生真实正文；5 个正式项已通过公开 API 删除，8021 端口已停止监听。
- 证据故障：真实数据完整保存在 `phases.a1...b3`、`initial_launch`、`revision_launch`，但正式断言要求的顶层 `transport`、`revision`、`cleanup` 和 `initial_launch.*.real_content/question_count` 别名未写入，因此机器断言 `KeyError`。
- 判定：evidence failure，不是产品失败，也无需重复慢链。重复运行不会增加产品证据，只增加 Provider 与内容生成成本。
- 最小修正：从本次真实 raw phase evidence 做确定性 schema 投影，并把同样投影写入脚本未来正常输出；所有字段必须由已有记录推导，禁止手填通过值。独立断言通过后即可把正式 HTTP/运行时就绪升级为 verified。

### 2026-07-17 顶层投影仅剩 transport 命名别名

- 独立结果：initial/revision/cleanup 顶层字段已可追溯生成，服务停止和内容证据正确。
- 剩余证据故障：`transport` 当前写为 `{"http_router_jobs": [initial_job_id, revision_job_id]}`，而正式断言要求 `draft_generation="http_router_jobs"` 与 `revision="http_router_jobs"`。真实两个 job ID 已存在，只有 schema 命名不一致。
- 最小修正：保留原 job ID 列表作为追溯信息，增加两个正式命名字段，并同步脚本未来输出；不得修改其他字段或重跑链路。

### 2026-07-17 正式 HTTP 完整链通过，R10 直达表达矩阵先行

- 独立结果：正式 Router HTTP 初版、parent-linked 修改、正式项采纳、学习/复习真实正文、基础练习真实题目和修改后内容启动均已通过机器断言；证据 schema 已收口。
- 浏览器边界：最终代表性场景仍只能由 Codex 自己真实浏览器操作升级为页面 verified；runner 不再执行浏览器。数量按 2026-07-17 用户授权精简为 5 条初版、3 条修改和 1 条三类型启动链；禁止改用 standalone Playwright、Computer Use 或 runner 页面任务冒充。
- 最小继续节点：先用隔离正式 HTTP `/api/router/jobs` 对无需画像准备的 8 条直达表达做真实 Provider 语义矩阵：无范围 A/B、整书 A/B、章节 A/B、书+章节+主题 A/B。无范围必须具体澄清，其余必须生成真实 scope 草案；只保存脱敏证据，不启动慢内容链、不改产品。
- 停止条件：任一表达出现内部错误、泛澄清、错误 scope、单章冒充整书或主题丢失，证据必须 failed 并保留第一断点；不得增加关键词补丁或临时链路。该矩阵只降低最终页面验收风险，不替代浏览器。

### 2026-07-17 R10 直达矩阵 7/8，明确教材章节的“学习”表达误分类

- 独立结果：无范围 A/B、整书 A/B、章节 B、书+章节+主题 A/B 共 7 条正式 Router HTTP 通过；唯一失败为 `学习《牙体牙髓病学》第15章` 被 LLM Router 判为 `clarification`，返回泛意图菜单而不是计划。
- 需求解释：R10 已把该结构明确列为章节计划验收表达，因此本场不是要求启动普通学习。与此同时，普通“系统学习某主题”和具体医学问答仍必须分别保持 `learning`、`direct_qa`，不能把所有含“学习”的请求改成计划。
- 最小修正：仅增强 MasterRouter 的 LLM classifier contract/examples，使“明确教材身份 + 章节号/标题、用于指定计划范围”的表达稳定归为 `study_plan`；不在 Python 添加教材名、章节号或关键词 heuristic，不改下游计划解析、KG、前端或执行链。
- 验收：两个不同真实教材的同结构表达经真实 HTTP 均进入 study_plan；两个结构不同的普通主题学习仍进入 learning；一个具体问答仍进入 Direct QA；原 8 条矩阵可只重跑失败场及结构不同正负例，不重复整书长草案。

### 2026-07-17 Router targeted 5/5 行为正确，去重 prompt 并收口 evidence

- 独立结果：真实 targeted HTTP 已证明两个 scoped-learning 正例、两个普通 learning 反例和一个 Direct QA 反例全部正确；MasterRouter 154 项通过，端口已释放。
- 未通过原因一：classifier prompt 在 intent 定义、边界说明和 critical rules 三处重复相同规则，并使用真实教材名示例。该实现虽然有效，但不符合用户要求的最小逻辑和无样例硬编码。
- 未通过原因二：evidence 使用 `scenarios` 字典，正式合同要求 `cases` 列表及每项 `expected_intent/actual_intent/status`，原样断言失败。
- 最小返工：只保留一处通用语义规则，使用 `<教材名>/<章节定位>` 占位符而非任何真实教材；不增加 Python heuristic。同步 targeted 脚本和 evidence schema，重新运行 5 条真实 HTTP，其他行为与文件不得改变。

### 2026-07-17 Router scoped-learning 边界通过，验证两类短续答

- 独立结果：prompt 已去重、无真实教材样例；targeted 5/5 与完整相关回归 707 项通过，scoped-learning Router 断点关闭。
- 下一最小节点：用两个全新正式 HTTP 会话分别完成“无范围请求→具体澄清→只回复真实章节标题”和“无范围请求→具体澄清→只回复真实书名”。第二句不得重复计划/安排/学习动词，必须消费 parent draft/history 后产生正确真实 scope 草案。
- 边界：只新增隔离脚本/evidence，不修改产品、不执行浏览器、不启动计划项或内容慢链。结果只验证正式多轮 Router/plan handoff，不替代最终页面。

### 2026-07-17 两类短续答 raw behavior 通过，仅补正式 evidence aliases

- 独立结果：章节回复与书名回复均在正式 HTTP 两轮会话中消费正确 parent/history；分别生成单章 3 项与整书 21 章/63 项 child draft，跨会话隔离通过。
- 证据故障：工件保留了完整 `round1_* / round2_*` raw 字段，但正式断言要求的简化 aliases 和顶层 `cross_case_isolation` 未投影，原样断言 `KeyError`。
- 最小返工：从已有 raw 事实生成 aliases，并同步脚本未来输出；不得重跑 Provider/整书草案、不得修改产品或其他证据。

### 2026-07-17 短续答 aliases 必须扁平位于 case 顶层

- 独立反证：首次 alias 任务写成 `case.aliases.kind/...`，而正式断言消费 `case.kind/...`；book case 只投影 kind/scope_mode，缺少 common first/second/child aliases。
- 最小修正：两个 case 均从 raw round 字段生成 common 顶层字段，再按 scope facts 生成各自 kind/scope_mode；可保留嵌套 aliases 作为追溯，但正式顶层必须完整。不得重跑或改其他内容。

### 2026-07-17 章节名/书名短续答通过，验证真实薄弱点三入口

- 独立结果：两类短续答正式 HTTP 证据已通过，未重跑整书草案。
- 下一最小节点：在隔离 LearningMemory 中用真实 KG core IDs 写入生产形态 `profile_state.weakness_summary` 与低分 `mastery` learner-memory slots，然后所有能力调用均走正式 HTTP：
  1. “我有什么薄弱点”返回真实证据概览；
  2. “生成关于我薄弱点的计划”生成 N 天/2N 项 review+basic；
  3. 无范围澄清后仅回复“我掌握得不好的那些内容”生成同一真实弱点计划；
  4. 无证据用户诚实返回无证据，不虚构计划。
- 边界：内部 `LearningMemory` 仅允许用于隔离前置数据准备和清理，不得用于查询/计划能力或绕过 Router；不改产品、不执行浏览器、不启动内容慢链。

### 2026-07-17 薄弱点正式 HTTP raw 行为通过但同范围与 evidence 合同返工

- 独立判定：`需要返工`（evidence failure）。概览、直接弱点计划、无证据负例和澄清续答四类正式 HTTP 均有真实 raw 行为；但 Case 3 的独立 fixture 只准备了一个 low-mastery 弱点，因此仅生成 1 天/2 项，未满足“与 Case 2 相同两个弱点范围、2 天/4 项”。
- 正式反证：任务规定的原样 JSON 断言仍因缺少 case 顶层 `kind`、顶层 `setup`、统计 aliases 和 `cleanup.temporary_db_removed` 报 `KeyError: kind`；脚本内部 passed 不能替代正式合同。
- 最小修正：仅让 Case 3 独立临时 DB 准备与 Case 2 相同的动态两类真实证据，并从两条 raw draft items 严格比较 scope/天数/类型；同步生成扁平正式 aliases 后实际重跑。禁止修改产品、测试、页面、默认 DB 或其他链路。

### 2026-07-17 Codex 浏览器首个薄弱点场通过，parent 修正分类 JSON 被截断

- 独立页面正向证据：Codex 使用独立临时 LearningMemory 与当前正式页面，完成“无范围请求→可读澄清→仅回复我掌握得不好的那些内容”，页面显示两个真实 KG 弱点、4 项 review+basic 计划卡和“打开学习计划”。
- 独立页面失败证据：同会话随后输入“范围保持不变，每天最多30分钟，把练习减少一些”，页面回到泛意图菜单。正式 job 记录的 classifier raw 只到 `{"intent": "study_plan", "plan_item_requested": false, "topic`，即 256 token 输出预算下 JSON 被截断；不是修正语义判断错误，也不是前端未传 user/parent。
- 失败分类：implementation failure。R10 首场初版页面可记局部 verified，但对应修正仍 failed，不能推进整体完成。
- 最小修正：仅在 MasterRouter classifier 收到非空 parent draft context 时提高单次输出预算；无 parent 保持 256。不新增重试、partial JSON parser、关键词、强制 parent route、接口或其他链路。

### 2026-07-17 parent 修正分类与正式证据独立通过，进入精简浏览器矩阵

- 独立结果：两个结构不同的 parent-linked 修改均经真实 `/api/router/jobs` 路由为 `study_plan`，生成不同 child draft 且 parent lineage 保持；无 parent 的普通学习与 Direct QA 同时保持。相关 MasterRouter 回归 163 项通过。
- 证据收口：`revision_cases` 已从原始五案例按 `case_id` 投影 `expected_intent/actual_intent/status/parent/child`，正式 JSON 断言通过；`run.log` 未重跑、8040 已释放、`__pycache__` 已清理。runner 最终失败标记仅来自 SessionEnd hook cancelled，不影响 Codex 独立验收结论。
- 下一节点：按用户授权执行 5 条非重复初版、3 条代表性修改和 1 条三类型启动页面链；同义重复由已通过的正式 HTTP 矩阵覆盖。浏览器只由 Codex 操作，runner 仅可准备隔离运行环境。
- 最终硬门槛：采纳计划并点击开始后，必须耐心等待正式既有 learn/review/basic 链返回页面可见真实正文或可答题目；不得创建临时产品链路、静态内容或 mock 冒充。

### 2026-07-17 浏览器运行环境首轮违反默认 DB 边界，已恢复并返工

- 独立反证：首轮 runtime manager 虽启动了 8040 和真实页面，但通过把隔离 DB 复制覆盖到工作区 `data/learning/learning.sqlite3` 实现 fixture；`stop` 又因跨进程丢失备份状态未自动恢复。这违反“不得触碰默认数据库”，环境验收失败，禁止进入浏览器。
- 已完成恢复：Codex 安全停止 PID 31664、释放 8040，从同目录 `learning.sqlite3.r10_browser_backup` 原位恢复默认 DB，SHA256 为 `D40D2BCF93EF2A5F70EF1A5CF94438CE6B63EC7BE5628D6ED2ED835AD2050ECB`，SQLite `integrity_check=ok`、13 tables；残留备份、manifest 和临时根均已清理。
- 最小返工：服务进程以临时数据根作为工作目录，并通过 project `PYTHONPATH/--app-dir` 导入正式 app，使硬编码相对默认路径自然落在临时根；完全删除 workspace DB swap/backup/restore 逻辑。必须证明 start/status/stop/start 全生命周期都不改变默认 DB 哈希。

### 2026-07-17 真隔离环境通过，浏览器发现计划链接丢失 user_id

- 环境结论：临时 cwd runtime 已通过独立验收，workspace 默认 DB 哈希持续保持 `D40D2B...50ECB`，无 backup，临时 DB integrity ok、13 tables，8040/PID 2424/page 200 均有效。
- 页面正向：Codex 真实页面完成“无范围→薄弱点短续答”，显示两个真实 KG 弱点和 4 项计划；随后 parent 修改成功生成同范围 3 项子计划，单项 30 分钟、练习 2→1，截图 `03-weakness-revision-plan-passed.png`。
- 页面断点：修改后“打开学习计划”真实 href 只有 `plan_draft_id`，没有当前 `user_id=codex-browser-r10-user`。学习计划页回落到 `demo` 并显示草案不属于用户，截图 `04-plan-link-user-context-failure.png`；无法采纳或点击开始。
- 最小修复：只在 Master Chat 生成学习计划链接时追加当前 `state.userId`，沿用 Learning Plan 已有 `user_id` URL 解析；不新增存储、接口或临时链路。必须增加前端合同测试并保持无 user 时既有默认语义。

### 2026-07-17 代表 learn 启动暴露固定行高卡片覆盖交互

- 用户授权后的验收精简：浏览器不再重复跑已被正式 HTTP 矩阵覆盖的同义措辞，只保留不同链路的代表场景；普通 learning、Direct QA 和必要的整书页面仍由 Codex 最终确认。
- 独立页面反证：同一天存在多个 42-core 大范围计划项时，`todo-item` 固定为 54px，而标题/元数据允许任意换行；后一个 article 覆盖前一个 article 的“开始学习/修改/删除”区域。DOM 中 learn href 与 user_id 均正确、链接 enabled/visible，但真实命中点属于下一卡片，点击会误触其它动作，不能以手工直达 URL 冒充通过。
- 需求与通用规则：固定高度计划行中的可变标题/元数据必须在自身网格列内有界裁切，不得侵入相邻行；完整文本仍保留在 DOM。修复不得依赖该教材、42 个 core、第三项或具体 plan ID。
- 最小返工：仅修改 Learning Plan canonical CSS 的文本 containment/ellipsis、HTML CSS 版本 token 和静态合同测试；不改 JS、后端、计划数据、布局结构或其它页面。Codex 在新资源生效后重新真实点击 learn 并等待正式正文。

### 2026-07-17 用户授权精简后的最终页面与隔离验收完成

- 浏览器用例边界：用户明确允许删除明显重复的页面验收。因此整书 A/B、章节/书名短续答 A/B 和其它 parent 修正措辞不再逐条重复页面操作；其真实 provider、scope、parent/child 和组合语义由已通过的正式 HTTP 矩阵承担。页面只验证不同 renderer/handoff/runtime 风险。
- 页面最终证据：薄弱点澄清续答生成两个真实 KG 弱点/4 项计划；同会话 parent 修改生成不同 child；草案跨页携带 user 并采纳；review 显示真实正文，basic 显示真实题目。卡片 containment 修复后，42-core learn 项真实命中点属于自身 SVG/anchor，点击后显示“成牙骨质细胞瘤”正式学习正文。普通“系统学习含牙囊肿的基础概念”保持学习模式并显示正文/PPT；具体问题直接返回 Direct QA 答案。
- 自动证据：最终 production-file Node 合同 76/76；此前相关 Python 回归 707 项和正式 HTTP 矩阵保持通过；本轮仅 CSS/HTML token/静态合同改变，没有后端回归失效因素。
- 环境收口：隔离服务 PID 2424 已停止，8040 释放，临时根与 manifest 删除；workspace 默认 DB 停止前后 SHA256 均为 `D40D2BCF93EF2A5F70EF1A5CF94438CE6B63EC7BE5628D6ED2ED835AD2050ECB`，`integrity_check=ok`、13 tables、无备份残留。
- 最终判定：R1–R10 全部 `verified`；没有 `cannot-prove` 或用户决策阻塞。计划完成，停用无人值守自动化。

### 2026-07-17 默认本地环境暴露 no-scope operation=null 兼容缺口并修复

- 用户真实反证：自动化停用后，用户在默认本地站点输入“给我制定个计划”，真实 provider 返回 `target_type=unknown`、`operation=null`；严格 selector validator 将其转为字符串 `None` 并报 `invalid_operation`，页面显示处理失败。此前完成声明遗漏了该 provider 合法 JSON 变体，现已由用户证据纠正。
- 根因：scope selector prompt 规定无范围使用 `target_type=unknown`，但没有规定此时仍须提供哪一个 schema operation；resolver 本可基于 unknown 返回澄清，validator 却先拒绝 null。
- 最小修复：prompt 明确 unknown 使用 `operation=all` 作为不选择任何 scope 的 schema 占位；validator 仅对 `unknown + null` 归一化为 `all`。所有能选择真实内容的 target 继续拒绝 null，未放宽事实范围校验。
- 独立证据：`ScopeSelectorValidationTests` 与 `ScopeSelectorParseFromTextTests` 共 28 项通过；默认本地 8086 重启后，用完全相同输入真实页面返回教材/章节/主题澄清，截图 `artifacts/chat-study-plan-runtime/no-scope-null-operation-clarification-passed.png`。R2/R8/R10 的 verified 状态在修复后恢复成立。

### 2026-07-17 用户默认薄弱点反例纠正并通过

- 用户真实反证：默认 8086 页面输入“薄弱点”仍显示 `Deterministic weakness plan preflight failed`，首项范围被报告为 `scope "None"`，过滤条件为 `document_id='None' / chapter='None'`。这使此前“R9 全部 verified”的结论对默认历史画像数据不成立，相关薄弱点入口降级为 failed。
- 根因与边界：默认画像保存历史 `kg://...` raw KG 节点 ID，而章节索引使用 `core:*` stable ID；`_enrich_weakness_resolved_scopes` 只按 stable ID 查章节，元数据在进入候选/预检前丢失。修复只在既有 enrichment 层增加 raw→真实章节元数据适配，保留 raw `kg_node_ids` 作为下游资源身份；未知 ID 不虚构章节、不绕过预检。
- 独立代码/数据证据：`_enrich_weakness_resolved_scopes` 读取默认 `demo` 画像得到 2 个 raw scopes，均带真实《牙周病学》/第十章元数据；对每个 raw scope 调用真实 `preflight_scope_resources` 均 `usable=True` 且命中 1 个 KG 节点。聚焦回归 `23 passed, 437 deselected`；raw/stable 子集 `16 passed`；`git diff --check` 通过。MasterRouter 全量单测的首个失败为既有显式 plan-item fixture 的 `learning_scope` 断言（35 passed 后失败），不涉及本轮 study_plan 修复，未将其误记为本轮通过。
- Codex 真实浏览器证据：重启 8086 载入最新代码，在当前页面输入“薄弱点”后显示计划模式、2026-07-17、60 分钟、4 个真实计划项（2 个 review + 2 个 basic），范围均为《牙周病学》第十章“牙周炎”，DOM 中没有 `scope "None"`、`document_id='None'`、`chapter='None'` 或 preflight failure；截图已在本轮直接展示给用户。artifact：`artifacts/chat-study-plan-runtime/weakness-raw-scope-fixed.png`（对话截图由浏览器运行时直接展示）。
- 当前判定：R9 薄弱点默认入口恢复 `verified`；其余 R1–R10 的既有证据不变。默认服务 PID 为 22688，浏览器页面保持打开供用户复看。

### 2026-07-17 无范围澄清文案补充薄弱点入口

- 用户反馈：原提示“请告诉我您想学习哪本教材的哪个章节”没有说明可按学习画像复习薄弱点，用户无法从澄清卡片发现该能力。
- 统一文案：后端正式澄清、编排提示示例和前端兜底均改为“请告诉我您想学习的教材章节或主题，也可以直接说‘薄弱点’，我会根据您的学习情况安排复习。目前支持《牙周病学》《口腔颌面外科学》等口腔医学教材。”不改变路由、范围解析、计划生成或页面交互。
- 独立证据：前端合同 76 passed；MasterRouter clarification/study_plan 聚焦 21 passed；允许文件 `git diff --check` 通过。默认 8086 重启后，以“给我制定个计划”真实页面复验，新提示完整显示“薄弱点”和基于学习情况安排复习，旧句不再出现。
- 当前判定：用户可见文案修正 `verified`，不改变 R1–R10 既有能力与验收结论。
