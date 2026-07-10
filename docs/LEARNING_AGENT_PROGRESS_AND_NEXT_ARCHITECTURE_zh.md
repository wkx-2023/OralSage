# OralSage 学习 Agent 进度与下一步交接

> 给队友 / Claude Code / Codex 换窗口前阅读。本文是人看的交接文档：讲清楚设计思路、当前状态、真实结果、不足和下一步建议。不要把本文写成函数名堆积、JSON 仓库或 smoke 日志。

## 0. 开发前必读

开发任何学习 Agent 相关功能前，先看：

- [docs/DEVELOPMENT_DECISIONS_README_zh.md](./DEVELOPMENT_DECISIONS_README_zh.md)  
  关键工程决策。重点：LLM 是主角，Python 是配角；`normalize()` 只修格式，`validate()` 只校验；验收必须展示真实生成结果；交接文档不能只堆中间产物。

- [docs/LEARNING_AGENT_ARCHITECTURE_zh.md](./LEARNING_AGENT_ARCHITECTURE_zh.md)  
  学习 Agent 总体架构。

- [docs/ORAL_COURSE_KNOWLEDGE_GRAPH.md](./ORAL_COURSE_KNOWLEDGE_GRAPH.md)  
  KG / KAG 数据结构参考。

任务说明必须写清楚：

```text
终态形状：输入、输出、LLM 负责什么、Python 负责什么
本次范围：只做终态的哪一部分
禁止清单：不能引入哪些固定脚本、中间格式、Python 内容补丁
验收展示：如果生成用户可见内容，必须展示真实生成结果
```

## 1. 当前整体设计

OralSage 当前要先做清楚一条正常学习链路，不急着把所有事情都包装成复杂 Master Agent。

当前主设计是：

```text
Orchestrator
+ Planner / Resource Factory / Tutor / QA / Assessment
+ Memory
```

原因是：学习页上的动作空间很小，用户不是在开放世界里乱逛，而是在一节一节学习。大部分路由天然由前端动作决定：

```text
输入框提问 -> QA
点击小测 -> Assessment / quiz flow
点击继续 -> 下一节 Tutor
```

所以当前阶段不需要强行做一个复杂主 Agent 来“智能调度”这些固定入口。真正需要 LLM 能力的地方，是 Planner、Resource Factory 的讲义生成、Tutor、QA、错因解释这些模块。

### 正常运行过程

一门课的正常链路应该长这样：

```text
1. 用户提出学习需求，例如“我想学牙周炎”

2. Planner 生成课程大纲
   - LLM 决定学习单元和顺序
   - Python 提供 KG / KAG / evidence，做 normalize / validate
   - 输出 outline + outline_evidence_map

3. Resource Factory 生成 canonical handout_sections
   - 每个 outline node 对应一个 handout section
   - LLM 基于证据写讲义
   - Python 只负责证据读取、targeted search、引用校验
   - handout_sections 成为后续 Tutor / QA / quiz / slides 的内容母本

4. Orchestrator 进入当前 section
   - 读取 current_outline_node + current handout_section + 用户状态
   - 调用 Tutor 讲当前小节
   - Tutor 同时生成 1-2 道 optional section quiz
   - 返回给前端：讲解正文、可选小测、用户下一步可选动作

5. 用户有三种常见动作
   - 在输入框提问：调用 QA
   - 点“小测”：展示本节题目，提交后调用 Assessment
   - 点“继续”：进入下一节 Tutor

6. 状态更新
   - Tutor 记录 exposure，不直接改 mastery
   - QA 记录 confusion signal，不推进课程
   - Assessment 批改后产生 weak_kg_node_ids / grading
   - Orchestrator / Memory 按 KG node 更新 mastery_by_kg_node
```

这条链路的关键边界：

```text
Outline 只是学习路径和证据索引，不是正文来源。
Handout sections 是内容母本。
Tutor / QA / quiz 必须基于 handout section 和证据，不要直接根据 outline 生成内容。
mastery 按 KG node 更新，不按 handout key point 硬绑。
key point 可以用于讲解和出题，但不一定等于 KG node。
```

### 组件职责

```text
Planner:
  决定学什么、按什么顺序学、每节绑定哪些证据。

Resource Factory:
  把每个学习单元写成可教学的 canonical section。

Tutor:
  按当前 handout section 讲解本节，并准备 optional section quiz。

QA:
  回答用户在当前学习上下文中的问题；必要时做受控 targeted search；不推进课程。

Assessment:
  批改小测，解释错因，给 Orchestrator / Memory 提供 weak KG nodes。

Memory:
  确定性更新 session progress、events、mastery_by_kg_node。

Orchestrator:
  承接前端动作，决定调用 Tutor / QA / Assessment / continue。
```

### 不要误解成固定脚本

Orchestrator 是产品流程入口，不是固定脚本冒充 Agent。它只负责“用户点了什么就进入哪个模块”。内容和判断仍应由对应模块完成。

禁止把这些事情写成固定规则：

```text
讲义正文
Tutor 讲解
QA 答疑
错因解释
Planner 学习单元和顺序
```

同时也不要为了显得智能，强行引入复杂主 Agent。当前阶段的重点是把正常学习链路做稳定、证据可追溯、状态边界清楚。

## 2. 当前总体状态

已经有的底座：

- Learning Memory：SQLite 保存 session、outline、resources、events、answers、mastery。
- Orchestrator：已有 start / step / interruption / answer / continue / state 的 API 链路；当前不按复杂 Master Agent 理解。
- LLM Policy / Intent Router：能展示 raw_text -> decision -> guarded execution；只适合自由入口或调试，不替代明确按钮动作。
- Planner：已切到 LLM Agent Planner 主路径，能输出 evidence pool 和 outline_evidence_map。
- Resource Factory：已有单节 handout section 生成和多节并行 handout 生成 MVP。
- Tutor：固定 handout 输入稳定，前 2 节全链路 smoke 阶段性通过。
- QA：当前 section 答疑单模块 smoke 阶段性通过，能基于已有 handout_section + evidence / targeted search 生成真实答疑；同一 section 内支持轻量追问上下文；QA 后可由 Orchestrator / Memory 记录 profile / mastery 信号。
- Assessment：已从 rule_based 泛泛判错升级为“Python 确定性判分 + LLM 错因分析”阶段性通过；错题不推进，能写入 profile / mastery。

当前可以说：

```text
后端状态链路初步跑通。
Planner Agent 主路径阶段性可用。
Resource Factory -> Tutor 前 2 节最小闭环阶段性跑通。
Tutor 在固定 handout 输入下稳定生成完整讲解和 optional section quiz。
QA 当前 section 答疑阶段性通过：不推进课程，必要时受控检索，失败不冒充 PASSED。
Assessment 错题反馈阶段性通过：答错后生成 LLM 错因分析，记录错题上下文，并更新 profile / mastery。
后端最小体验链路 API smoke 已通过：start -> step -> QA -> wrong answer -> continue -> next Tutor -> correct answer -> state。
```

当前不能说：

```text
生产级完整课程讲义已完成。
Tutor 连续式真实讲课已经完全完成。
QA 已经生产级稳定支持任意多轮答疑。
Assessment 已经生产级稳定支持所有题型和复杂病例分析。
复杂开放式调度已经完成。
前端输入框 / 小测 / 下一页体验已经完整接好。
slides / video / mindmap / quiz 已经从 handout 正式派生。
```

## 3. Planner / 大纲模块

### 设计思路

Planner 只负责回答：

```text
这门课应该按什么顺序学？
每个学习单元为什么存在？
每个单元绑定哪些 KG / KAG 证据？
```

Planner 不写讲义正文，也不直接给 Tutor / QA 当内容来源。

主路径是：

```text
topic / raw goal / user_state / evidence_pool
-> LLM Agent Planner 自主规划学习单元和顺序
-> Python normalize / validate
-> Python 按 LLM 选择的 ids 构建 outline_evidence_map
-> 输出 outline + outline_evidence_map + planning_trace
```

### 当前状态

Planner 已接入 LLM Agent Planner 主路径，并完成第一轮证据硬约束收紧。

当前 Agent Planner 成功路径要求每个 node 至少有：

```text
kg_node_ids
evidence_chunk_ids
sequence_reason
```

可选增强证据：

```text
kg_edge_ids
concept_ids
question_ids
```

最近审计结果：

```text
牙周炎 / 根尖周炎，各跑 2 次
Agent Planner success: 4/4
truncated_json_count: 0
validation_failed_count: 0
Agent 成功路径 node_without_kg_node: 0
Agent 成功路径 node_without_chunk: 0
```

这说明 Planner 主路径阶段性可用，但不等于 Planner 完全完成。

### 当前真实输出形态

一次 `牙周炎` 成功规划大致是这种形态：

```text
课程标题：牙周炎的系统性学习：从疾病本质到综合管理

总体顺序：
先建立疾病概念和病理特征，再介绍分类框架；随后围绕慢性牙周炎展开病因、表现和诊断，再对比侵袭性牙周炎；最后扩展到全身相关性和长期治疗维护。

学习单元示例：
1. 牙周炎的疾病本质——从牙龈炎到牙周支持组织破坏
   顺序理由：先建立疾病边界，后续分类、机制和诊断都依赖这个判断。
   证据：有 kg_node_ids，有 evidence_chunk_ids，有 kg_edge_ids。

2. 牙周炎临床分类的演变——从成人牙周炎到新分类体系
   顺序理由：定义之后讲分类，给后续慢性、侵袭性和全身疾病相关牙周炎建立框架。
   证据：有 kg_node_ids，有 evidence_chunk_ids；kg_edge_ids 可能为空。

3. 慢性牙周炎的始动因素——菌斑生物膜与局部促进因素
   顺序理由：分类之后先讲最常见类型的病因，为临床表现和防治原则打基础。
   证据：有 kg_node_ids，有 evidence_chunk_ids，有 kg_edge_ids。
```

说明：

- `question_ids` 不是每个节点都有，目前是可选证据。
- `kg_edge_ids` / `concept_ids` 覆盖还不均匀，属于质量优化项。
- `kg_node_ids` 和 `evidence_chunk_ids` 是 Agent 成功路径硬约束。
- 不要固定用 `node_1` / `node_2` 判断内容质量，Planner 每次生成标题和顺序可能波动，应按标题和证据质量审。

### 已废弃流程

以下是废弃流程，只能 fallback / debug / smoke，禁止用于主路径开发，也禁止继续在上面扩展讲义、Tutor、QA 或 Resource Factory 主能力：

```text
固定维度生成大纲
固定顺序生成学习路径
LLM 只改标题和 learning objectives
旧 fixed-dimension outline 作为 Agent Planner 的强制 skeleton
```

### 当前不足

- Planner 很慢，常见 60-110 秒，主要耗时在远端 LLM。
- prompt 仍偏长，后续需要压缩 evidence input。
- edge / concept / question 覆盖不均。
- fallback 仍保留旧固定维度路径，只能兜底，不能当正式内容来源。
- 审计样本还少，只覆盖 `牙周炎`、`根尖周炎` 各 2 次。
- 大纲医学质量仍需人工抽样复核。

### 下一步建议

Planner 当前不是最急的开发点。后续可以做：

- 增加分步耗时 trace。
- 扩大审计主题集。
- 优化 prompt 和 evidence input，降低耗时。
- 继续提升 edge / concept 覆盖。

但当前更优先的是讲义质量，因为 handout_sections 是后续内容母本。

## 4. Resource Factory / 讲义模块

### 设计思路

讲义不是普通资源，而是后续内容母本。

当前架构决策：

```text
Outline = 结构、顺序、进度指针、证据索引入口
Handout / handout_sections = canonical content source
Slides / video / mindmap / quiz / Tutor / QA = 从 handout_sections 派生
```

正确链路：

```text
Planner 输出 outline + outline_evidence_map
-> Resource Factory 按节读取证据、必要时 targeted search
-> LLM 写出结构化 section
-> Resource Factory 汇总 sections，按 Planner 原始顺序排序
-> 后续 Tutor / QA / slides / video / mindmap / quiz 从 handout_sections 派生
```

当前代码位置仍主要在 `backend/services/learning/resource_factory.py`，但文档和任务说明里应把讲义主能力称为 Resource Factory 的 handout_sections 生成能力。旧 Resource Factory 直接从 outline 生成多种资源的口径已经废弃。

Python 在这里负责：

```text
执行证据读取
执行 targeted search
校验证据引用是否合法
汇总 source_index
并行调度 section 生成
```

Python 不负责：

```text
写讲义正文
补医学内容
把 outline title 扩写成正文
润色 LLM 返回的讲义
```

### 已废弃流程

以下是废弃流程，禁止用于讲义主路径开发，也禁止继续在上面扩展 Tutor、QA、slides、video、mindmap 或 quiz：

```text
只根据 outline title 生成讲义正文
用 Python 模板拼讲义正文
旧 skeleton handout 当作正式讲义
slides / video / mindmap / quiz 直接从 outline 派生正文内容
```

### 当前状态

Resource Factory 单节 handout section 当前流程：

```text
Round 1：LLM 判断证据是否够、要读哪些证据、是否需要 targeted search
Python：只执行证据读取和检索
Round 2：LLM 基于证据写 section
Python：normalize / validate，校验证据 id
```

Resource Factory 多节 handout 生成当前流程：

```text
调用 Planner 得到 outline 和 evidence map
按 node 分节生成 handout section
支持并行，目前默认 max_workers=2
并发完成后按 outline 原始顺序重排
单节失败不会拖垮整份 handout，整份会变成 partial
汇总 source_index，包括 targeted search 新增 chunk ids
```

最新验收结果（2026-07-05，全链路最小闭环）：

```text
命令：
python scripts/smoke_test_learning_tutor.py --topic 牙周炎 --compare-sections --show-teaching --show-timing

结果：
Planner -> Resource Factory -> Tutor 前 2 节最小闭环连续 2 次通过。

Section 1：牙周炎的定义与分类
handout_status: llm_generated
evidence_status: sufficient
key_points: 4
tutor_status: llm_generated
teaching_chars: 1237
questions: 2
is_first: true

Section 2：牙周炎的病因与微生物学
handout_status: llm_generated
evidence_status: sufficient
key_points: 4
tutor_status: llm_generated
teaching_chars: 1178
questions: 2
is_first: false
```

注意：

- 这只验收了前 2 节最小闭环，不是完整课程级 handout 稳定性。
- 不能把“前 2 节 smoke 连续通过”包装成所有主题、完整课程或生产级稳定。
- 本轮已确认 Tutor guard 正常：handout failed 时 Tutor 会 skipped / failed，不会从 outline title 编讲解。
- 全链路曾失败一次，真实 trace 指向 Round 2 `likely_truncated`；`max_tokens` 从 6000 调到 12000 后连续 2 次通过。

### 当前真实讲义样例

下面是最近一次 `牙周炎 node_1` 的 LLM 真实生成 section 正文。这里仅把后端 JSON 的 `content` 字段按讲义形态展示；未人工润色、未补写。这个样例说明：讲义厚度已比上一版明显提升，key point 之间也开始出现衔接表达。

#### 牙周炎的定义与分类演变

牙周炎是口腔常见病之一，准确认识其定义和分类是学习牙周病学的基石。本节将从疾病本质出发，阐明牙周炎的核心病理特征，并梳理从传统"成人牙周炎"到2018年新分期分级体系的分类演变，帮助同学们建立系统的诊断思维框架。

##### 1. 牙周炎的定义与疾病本质

牙周炎是一种发生于牙齿支持组织（包括牙周膜、牙槽骨和牙骨质）上的慢性炎症性感染性疾病。与仅局限于牙龈的牙龈炎不同，牙周炎的破坏进程累及牙周膜和牙槽骨，导致附着丧失和骨吸收，是成人牙齿丧失的主要原因。其始动因素是口腔细菌在牙面聚集形成菌斑生物膜，激惹宿主的免疫炎症反应。牙周炎的进展速度和严重程度取决于宿主遗传背景与局部及全身行为因素，如吸烟、精神压力和牙石等局部因素。因此，牙周炎的本质是细菌感染驱动的、由宿主免疫炎症反应介导的牙周组织破坏——这一认知直接决定临床治疗的基本原则：控制菌斑这一始动因素，同时管理宿主炎症反应以减缓组织破坏。在学习过程中，应始终把握"感染→炎症→破坏"这一主线，为后续理解牙周炎的不同类型和防治策略奠定基础。

##### 2. 牙周炎分类的历史演变

对牙周炎进行分类是为了更好地反映疾病生物学特性、指导治疗并判断预后。1989年的分类主要依据发病年龄和进展速度，将最常见于35岁以上成人的缓慢破坏性病变称为"成人牙周炎"，而将发生于年轻个体的快速进展型称为"早发性牙周炎"。这一分类简便实用，但未能充分体现病理生物学差异。1999年分类引入了"慢性牙周炎"和"侵袭性牙周炎"等概念，试图以病因和病理特征区分，然而在实际应用中暴露出两者间的诊断标准模糊、缺乏独立的病理生物学依据等问题，且临床决策上无法明确区分"慢性"与"侵袭性"——两者的组织破坏终点相似，均为牙槽骨丧失和牙周附着丧失。这些局限促使学术界转向更综合的分类标准，最终催生了2018年新分类——这是一次范式转移，旨在解决当前分类的科学研究局限和临床痛点。

##### 3. 2018年牙周病新分类框架及分期分级要点

2018年牙周病和种植体周病新分类取消了"慢性"和"侵袭性"的区分，将牙周炎视为一个单一疾病实体，再按临床表现分为三种类型：坏死性牙周病、反映全身疾病的牙周炎，以及最常见的"牙周炎"。对于"牙周炎"这一类型，进一步采用分期（Staging）和分级（Grading）来刻画疾病状态。分期（I～IV期）主要依据就诊时严重程度（临床附着丧失、骨吸收百分比、探诊深度、根分叉病变缺牙数等）和治疗复杂程度来指导治疗方案强度；分级（A、B、C级）则通过评估进展速度、危险因素控制状况（如吸烟、血糖水平）和全身健康影响，来判断疾病进展风险，从而决定预后。例如，一位附着丧失轻微、无系统疾病史的患者可能分为I期A级，而伴有快速骨丧失和血糖控制不佳的患者可归为IV期C级。这一体系强调了个性化治疗和长期管理，与当代精准医学理念高度契合。作为口腔医生，掌握分期分级思维，才能对牙周炎患者进行标准化评估和精准干预。

##### 常见混淆

许多学生和基层医师认为慢性牙周炎和侵袭性牙周炎是截然不同的两种疾病，可根据患者年龄或进展速度简单归类。

实际上，1999年分类中的两者在病理生物学上没有明确界限，两类间重叠特征多见，且从影响组织学改变看难以区分。2018年新分类是基于大量证据将其合并，转而用分期分级反映疾病严重度和进展风险，更符合临床实际。

##### 下一节

明确了牙周炎的定义和分类后，接下来我们将深入探讨导致牙周组织破坏的直接原因——牙菌斑生物膜的结构以及宿主免疫炎症反应的核心机制，理解这些病理过程是掌握牙周炎防治策略的关键。

最新全链路 smoke 的真实 Tutor 输出已展示在完成报告中：Section 1 完整讲解“牙周炎的定义与分类”，Section 2 完整讲解“牙周炎的病因与微生物学”，两节均为 `llm_generated`，讲解与题目分离。

### 当前不足

- 前 2 节最小闭环已连续通过，但完整课程级 handout 仍未验证。
- 多主题稳定性仍未验证，目前只重点验收了 `牙周炎` 前 2 节。
- Round 2 截断问题已通过 `max_tokens=12000` 暂时止血，但这会增加成本和耗时；后续可能需要压缩输出或拆分生成。
- 讲义正文厚度已改善，但仍需继续审计医学事实是否都有 chunk 原文支撑。
- 性能仍慢：Planner 约 60-110 秒，两节并行 handout 总耗时约 150-190 秒。
- 当前没有缓存、完整 handout 持久化、版本管理和复用接口。

### 2026-07-05 新增：handout_sections 后台预生成缓存

已实现 handout_sections 的文件持久化缓存层，解决用户点击"继续"进入下一节时临时生成讲义导致的长等待问题。

新增文件 `backend/services/learning/handout_cache.py`，存储路径为 `data/learning/sessions/{session_id}/handout_sections/{outline_node_id}.json`。

当前链路：`start_lesson` 完成后同步生成第一节 handout 并写 cache，同时启动后台线程使用 `ThreadPoolExecutor(max_workers=2)` 预生成剩余节点的 handout_sections。`next_step` 优先读 cache（`handout_cache_hit=True`），cache miss 时同步兜底（`cache_miss_sync_generation=True`）。QA 通过 `_read_existing_handout_section` 优先读 file cache 再读内存 cache，不触发 Resource Factory 重生成。

cache record 结构及 trace 字段详见 `handout_cache.py`。

### 2026-07-05 新增：JSON mode 诊断能力

已确认 Round 1 / Round 2 都传了 `response_format={“type”: “json_object”}`。最新改动：

- 新增 `_capture_raw_diag()` — 抓取 raw response head（前 300 chars）/ tail（后 200 chars）/ len / starts_with_brace / ends_with_brace / is_empty
- Round 1 parse 失败 → **retry 一次**；Round 2 normalize 失败 → **retry 一次**
- 失败 trace 包含：`used_json_mode`, `raw_response_len/head/tail`, `is_empty`, `starts_with_brace`, `ends_with_brace`, `retry_attempted`, `retry_succeeded`, `retry_error`（API fail/timeout/quota 可见）
- `used_json_mode=true` 只在实际调用了 LLM 时写入 trace（`no_llm_api_key` / `llm_client_import_failed` 不会标 true）
- 失败分类速查：

```text
is_empty=True          → 空响应（provider 返回空，可能是 rate limit / quota / 服务端错误）
starts_brace + !ends   → 截断（max_tokens 不够，JSON 未写完）
!starts_brace          → 非 JSON（provider 无视 json_object mode，返回文本/错误/HTML）
starts_brace + ends    → 合法 JSON 边界但内部语法错误或 schema 不匹配
                        但仍 parse 失败
```

已观察到一次真实失败样本：

```text
failure_mode: likely_truncated
round: Round 2
raw_len: 4963
starts_with_brace: true
ends_with_brace: false
判断：max_tokens=6000 不够，JSON 被截断
处理：Round 2 max_tokens 调到 12000
结果：之后全链路 compare-sections 连续 2 次通过
```

仍需继续积累失败样本，确认是否还存在 empty / non_json / schema_mismatch / provider_error。

### 下一步建议

```text
优先级 1：继续积累 Resource Factory 失败样本，用 _capture_raw_diag 分类诊断
  → 确认 provider json_object mode 有效性
  → 如果截断继续出现 → 优先压缩输出或拆分生成，而不是继续无限增加 max_tokens
  → 如果非 JSON 为主 → 加强 parse 容错（去除 code fence 后的容错提取）
  → 如果空响应为主 → 排查 API quota / rate limit / 服务端稳定性

优先级 2：做完整课程级 handout 验收和多主题抽样
优先级 3：缓存、持久化、版本管理，降低重复生成成本
```

## 5. Tutor 模块

### 当前状态（2026-07-05 更新）

Tutor 已完成第一轮修复，**自身在固定 handout 输入下 STABLE**。

已完成：
- `section_position` 上下文：Tutor 知道当前是首节/中间节/末节
- **第一节完整讲解**：第一节不再只是开场导语，而是和其他节一样展开所有 key_points 的完整教学
- **跨节承接**：非第一节用”上一节我们了解了……”承接，不重复”你好呀，我是 OralSage”
- **teaching_text / questions 分离**：讲解正文不含题目，题目只在 `questions` 字段
- **一对一语气**：禁止群体课堂语气（”同学们好””请大家”）
- **validator guardrail**：`validate_tutor_teaching_text()` 检查最小长度、key_point 覆盖、shell pattern（纯导语/预告会被拒绝）
- **handout 无效 guard**：handout 不是 `llm_generated` 或无 key_points 时，Tutor 直接返回 `failed` + `missing_valid_handout_section`，不调 LLM，不 fallback 到 rule-based
- **固定 fixture 验收**：`scripts/smoke_test_tutor_fixture.py` 用静态 handout 测 first_section 和 non-first_section，不依赖 Planner / Resource Factory

固定 fixture 验收结果（真实输出摘要）：

```text
Section 1 (first):  teaching=1334 chars, questions=2, tutor_status='llm_generated'
  开场: “你好呀，我是 OralSage。现在带你学习牙周炎的病因与发病机制。”
  覆盖 4/4 key_points，回应 common_confusions，引用 clinical_example
  NOT just an intro — full lecture

Section 2 (non-first):  teaching=1423 chars, questions=2, tutor_status='llm_generated'
  开场: “上一节我们了解了牙周炎的定义、分类和流行病学，这一节我们深入看它的病因和发病机制。”
  不重复自我介绍，承接上一节内容
  覆盖 4/4 key_points
```

### Tutor 当前边界

```text
Tutor 自身（固定 handout 输入）:  STABLE ✅
前 2 节全链路 compare-sections: 阶段性通过 ✅
  - 只要拿到合法的 handout_section，Tutor 就能生成完整教学
  - handout failed 时 Tutor guard 会拦截，不会从 outline title 编讲解
  - 完整课程级、多主题稳定性仍未验证
```

### 交互连续性

已完成：
- 第一节不是课程预告，第一节本身就是完整教学
- 非第一节承接上节，不重复自我介绍
- 结尾自然收束，不机械重复固定模板
- `available_user_actions = [“ask_question”, “take_quiz”, “continue”]`
- `expected_user_action = “choose_next_action”`

### 当前不足

- Tutor 自身不再是当前主要阻塞点。
- 前 2 节全链路已阶段性通过，但线下 fixture 和两节 smoke 都不代表所有主题、完整课程稳定。
- Tutor 质量仍取决于 Resource Factory 生成的 handout_section 质量。
- 未做 QA / Assessment 的深度交互

### 下一步建议

Tutor 自身可以先收口，不再继续追加 prompt 或 guardrail。下一步优先保证完整课程 handout / 多主题稳定性，再接 QA / Assessment 到主链路。

## 6. QA 模块

### 当前状态

QA 当前 section 答疑已完成一轮单模块验收，状态是阶段性通过。

已完成：

- 输入是用户原始问题、current_outline_node、已有 current handout_section、当前 evidence、用户画像。
- QA 分两步：先由 LLM 做 QA Plan，判断问题范围和是否需要 targeted search；Python 只执行证据读取 / targeted search；再由 LLM 做 QA Answer。
- LLM 输出结构化 JSON，代码调用使用 JSON mode。
- QA 不推进 `current_outline_node_id`，也不返回 `next_outline_node_id`。
- QA 不在用户提问时触发 Resource Factory 重生成；Orchestrator 只读取已缓存的 handout_section。
- `trace.uses_handout_section` 只在 handout_section 真实有效时为 true。
- handout 无效时标记 `degraded_without_handout=true`，不能把 degraded 结果包装成完整 PASSED。
- smoke 验收只接受 `qa_status="llm_generated"`；`fallback_rule_based` / `failed` 都不能算通过。
- QA 支持同一 current section 内的轻量追问上下文：Orchestrator 只传当前 `outline_node_id` 最近 3 条 QA event；用户进入下一节后不再携带上一节 QA，避免浪费上下文和串题。

### 目标架构

QA 应消费：

```text
raw question
current_outline_node
current handout_section
current lesson context
outline_evidence_map[current_node] / used_evidence
targeted search results（必要时）
user_profile / mastery
```

QA 的证据策略：

```text
1. 先读 current handout_section
2. 再读 current node 的 used_evidence / outline_evidence_map
3. 如果仍不足，再由 LLM 决定是否 targeted search
4. 如果 search 后仍不足，明确说 evidence insufficient
```

QA 可以检索，但不能变成无限搜索引擎。检索应限制为：

```text
问题与当前小节 / 相邻小节 / 当前课程 topic 相关
最多 2-3 个 query
每个 query 必须有 purpose
每个 query top_k 小而明确
search results 必须进入 used_evidence / trace
```

由 LLM 生成结构化答疑：

```text
answer
question_scope = in_current_section / adjacent_section_related / course_related_but_not_current / out_of_scope
related_kg_node_ids
confusion_kg_node_ids
used_evidence
evidence_status
targeted_search_used
should_reinforce
advance_progress=false
```

### 本轮验收结果

本轮 smoke 使用当前 section 的真实 handout_section，handout 状态为 `llm_generated`，包含 4 个 key_points，且 key_points 有 heading / explanation / evidence_refs。

验收覆盖两个问题：

```text
QA A：当前小节内问题。
QA B：当前 topic 相关但需要 targeted search 的问题。
```

结果：

```text
QA A: qa_status = llm_generated
QA B: qa_status = llm_generated
trace.uses_handout_section = true
degraded_without_handout = false
targeted_search_used = true
advance_progress = false
next_outline_node_id 不存在
完成报告已展示完整 answer 原文，未概括、未润色。
```

本轮可以说：

```text
QA 单模块已能在 current section 上基于 handout + evidence / targeted search 做真实答疑。
QA 不再是 placeholder。
```

本轮不能说：

```text
QA 已经完成生产级多轮答疑。
QA 已经和前端输入框完整闭环。
QA 已经完成跨节长期聊天记忆。
QA 能脱离 handout_section 任意回答医学问题。
```

### 当前不足

- 仍只验收了当前 section 两个问题，不代表多 topic、多轮连续问答稳定。
- 当前 QA 依赖 Orchestrator 已缓存 handout_section；完整课程级 handout_sections 的持久化读取还要继续梳理。
- targeted search 已接入，但 search 质量、证据排序和 used_evidence 引用质量还需要更多样本审计。
- QA 后可通过 Orchestrator / Memory 更新 profile / mastery 信号；但这只是保守的当前 section 学习信号，不是跨课程长期画像系统。
- QA 的 recent_qa_context 只做同节追问，不是跨节长期对话记忆；不能包装成完整聊天记忆能力。
- 前端输入框 -> QA -> 回到当前 Tutor 页面这条真实用户链路还没完整体验。

### 下一步建议

下一步不要继续堆 QA prompt 小修，优先把用户可体验链路串起来：

```text
当前 Tutor section
-> 用户输入框提问
-> QA answer
-> 回到当前 section，不推进课程
-> 用户可继续提问 / 做小测 / 下一节
```

同时补一条降级验收：如果 handout_section 缺失或无效，QA 必须明确 degraded，不允许偷偷重生成讲义，也不允许把 degraded 当 PASSED。

## 7. Assessment 模块

### 当前状态

Assessment 已完成一轮错题反馈链路修复。当前主路径是：

```text
Python 确定性判分
-> LLM 生成错因分析 / 正确答案解释 / 误区归因 / 补救建议
-> Orchestrator 记录错题上下文
-> Memory 更新 profile / mastery_by_kg_node
```

Assessment 仍不负责自己推进页面。答错时 `recommendation.action="reinforce"`，`current_outline_node_id` 保持不变；是否继续、提问或重做，由用户动作和 Orchestrator 承接。

### 目标架构

确定性判分可以保留，但医学解释和教学反馈必须由 LLM 生成：

```text
错因解释
weak point 归因
reinforcement suggestion
weak_kg_node_ids 建议
```

题目和解释应优先来自 handout_sections 的 key_points / common_confusions / used_evidence，而不是直接从 outline 标题生成。

### 本轮验收结果

来自 `smoke_test_learning_assessment.py` 的验收结果：

```text
V1:  is_correct=False
V2:  selected_option=A, correct_option=B
V3:  llm_generated + fallback_rule_based=False 一致
V4:  LLM 字段非空
V5:  错题不推进节点
V6:  Profile 更新（weak_points + patterns 增加）
V7:  Mastery 更新（按 kg_node_ids）
V8:  QA 不推进节点
V9:  QA 后 Profile 进一步更新
V10: 最终节点未变化

10/10 通过
```

本轮可以说：

```text
Assessment 已不再只是泛泛 rule_based 判错。
错题反馈主路径可以生成 LLM 错因分析。
答错题后会更新 user profile 和 mastery_by_kg_node。
错题上下文可供同 section 后续 QA 使用。
错题和 QA 都不推进 current_outline_node_id。
```

本轮不能说：

```text
Assessment 已生产级支持所有题型。
Assessment 已完成病例分析题、简答题等开放回答评估。
Assessment 已完成跨多节、多次答题的长期掌握度分析。
QA 追问错题的完整 answer 原文已在本文档展示。
```

注意：本轮粘贴到交接区的报告完整展示了 Assessment 错因分析原文，但“用户追问错题后的完整 QA answer”没有实际正文，只贴了状态说明。因此本文只把 Assessment 的真实用户可见内容作为验收样例；后续如果要把错题追问 QA 作为内容验收，必须补贴完整 QA answer 原文。

### 错题反馈真实输出

以下内容来自 `smoke_test_learning_assessment.py` 第一轮 LLM 生成结果。未润色、未改写。

```text
is_correct:            False
score:                 0.0
selected_option:       A
correct_option:        B
fallback_rule_based:   False
content_status:        llm_generated

【option_error_analysis】(LLM 真实生成)
用户选 A（遗传因素）。这个选项的错误在于：遗传因素虽能影响牙周炎的易感性，
通过影响宿主的免疫反应和炎症反应来间接影响疾病进展，但它不是牙周炎的核心
始动因素。牙周炎的必要条件是牙菌斑生物膜的长期堆积，没有菌斑就不会发生
牙周炎。因此，遗传因素不是核心始动因素。

【correct_answer_explanation】(LLM 真实生成)
正确答案 B（牙菌斑生物膜）是正确的，因为讲义明确指出牙菌斑生物膜是牙周炎
的核心始动因素。牙菌斑中的微生物及其产物会引发宿主的免疫炎症反应，导致
牙周组织破坏。虽然吸烟和糖尿病等危险因素会加速疾病进展，但牙菌斑是不可
或缺的始动因子。

【misconception_summary】(LLM 真实生成)
这道题反映出学生可能对"始动因素"和"危险因素"的概念区分不清，将危险因素
误认为核心始动因素。

【remedial_suggestion】(LLM 真实生成)
建议回顾讲义中关于牙周炎病因的部分，明确区分始动因素（牙菌斑生物膜）
与局部/全身危险因素（吸烟、糖尿病、遗传等）。可以思考：如果没有菌斑，
即使有遗传易感性和吸烟习惯，是否会患牙周炎？这有助于理解必要条件与
促进因素的区别。

related_kg_node_ids: ['kg://牙周病学/010-牙周炎/core_1']
recommendation.action: reinforce
```

### Profile / Mastery 更新

答错前：

```text
confidence: 0.2
known_weak_points: []
mistake_patterns: []
mastery_by_kg_node: 0 entries
current_outline_node_id: node_1
```

答错后：

```text
confidence: 0.45
known_weak_points:
  - kg://牙周病学/010-牙周炎/core_1
  - kg://牙周病学/010-牙周炎/core_2
  - kg://牙周病学/010-牙周炎/core_5
  - kg://牙周病学/010-牙周炎/support_8
  - section:node_1
mistake_patterns:
  - LLM 生成的 misconception_summary 或 QA 疑问主题

mastery_by_kg_node: 4 entries（按 question.kg_node_ids）
[kg://牙周病学/010-牙周炎/support_8] score=0.22 conf=0.35 exp=0
[kg://牙周病学/010-牙周炎/core_5]    score=0.22 conf=0.35 exp=0
[kg://牙周病学/010-牙周炎/core_2]    score=0.22 conf=0.35 exp=0
[kg://牙周病学/010-牙周炎/core_1]    score=0.22 conf=0.35 exp=0

current_outline_node_id: node_1
```

QA 后：

```text
current_outline_node_id: node_1
QA 不推进课程。
QA 后 profile 会进一步记录疑问 / reinforce 信号。
```

### 当前不足

- 当前只验收了单选题错题反馈，不代表所有题型完成。
- 简答题、病例分析题、部分正确答案的开放式评分还没完成。
- 跨多节、多次答题的长期掌握度分析还没完成。
- QA 追问错题的完整 answer 原文没有在本轮交接报告中贴出，后续内容验收必须补全。
- 题目 schema 仍要继续和 Tutor / Resource Factory 派生题目统一，例如 `single_choice`、答案 `A/B/C/D`、`kg_node_ids`、证据引用。

### 下一步建议

Assessment 下一步不要继续做泛泛模板优化，优先补两件事：

```text
1. 在 terminal demo 中完整体验：
   Tutor -> quiz -> 答错 -> Assessment 错因讲解 -> 用户追问错题 -> QA -> 仍停留当前 section。

2. 扩展题型：
   支持简答题 / 病例分析题的 LLM grading、部分正确判断、多 KG node 误区归因和个性化补救建议。
```

## 8. Orchestrator

### 当前状态

Orchestrator 是学习链路主入口，但当前不把它包装成复杂 Master Agent。产品交互动作空间很小，按钮和输入框本身已经给出大部分意图。

### 目标架构

当前阶段采用：

```text
Orchestrator
+ Planner / Resource Factory / Tutor / QA / Assessment
+ Memory
```

真实用户链路：

```text
进入课程
-> 生成 / 读取 handout_sections
-> Tutor 讲当前小节，返回 optional section quiz

用户在输入框提问
-> QA 基于当前 handout_section + evidence / search 答疑
-> 不推进课程
-> 记录 confusion signal

用户点“小测”
-> 展示当前 section quiz
-> 用户作答
-> Assessment 批改
-> 更新 mastery_by_kg_node

用户点“继续 / 下一节”
-> 进入下一节 Tutor 讲解
```

因此路由可以非常简单：

```text
课程页输入框提交 -> ask_question -> QA
小测按钮 -> take_quiz / submit_answer -> Assessment
继续按钮 -> continue -> advance current_outline_node_id -> next section Tutor
```

只有自由入口（例如首页用户说“我想学牙周炎”）才需要轻量 intent recognition。

### 当前后端验收

已新增 `/api/learning/continue`，对应用户点击“继续 / 下一节”：

```text
POST /api/learning/continue
-> route({"type": "continue"})
-> decision.action = continue_to_next_section
-> Orchestrator 推进 current_outline_node_id
-> 调用下一节 Tutor
-> 返回 tutor_step
```

当前 API smoke 已跑通：

```text
POST /api/learning/session/start
POST /api/learning/step
POST /api/learning/interruption
POST /api/learning/answer      # wrong answer, 不推进
POST /api/learning/continue    # 推进到下一节并返回下一节 Tutor
POST /api/learning/answer      # correct answer, Assessment 推进
GET  /api/learning/session/{session_id}/state
POST /api/learning/route       # unknown input 只澄清，不执行
```

本轮验收结果：

```text
learning api smoke test passed
QA: qa_status = llm_generated, advance_progress = false
continue: decision.action = continue_to_next_section, advanced = true, next tutor content_status = llm_generated
state: answers = 2, mastery = 4, events = 11
```

后端人工体验入口：

```text
python scripts/demo_learning_agent_terminal.py --topic 牙周炎
```

这个脚本不是固定剧本。它每轮展示和前端一致的动作：

```text
1 / ask   提问 -> QA
2 / quiz  做当前小节小测 -> Assessment
3 / next  继续 -> 下一节 Tutor
4 / state 查看状态
0 / quit  退出
```

### 全链路真实生成内容

以下内容来自本轮 `python scripts/smoke_test_learning_api.py --topic 牙周炎` 的真实输出。未润色、未改写、未概括，作为后端最小体验链路跑通证据。

Tutor Step 1 Teaching Text:

```text
你好呀，我是 OralSage。现在带你学习「牙周炎的分类与流行病学基础」。牙周炎听起来可能有点陌生，但你一定听说过“牙周病”，它其实是一大类疾病的总称，不同类型的牙周炎表现和治疗策略差别很大。所以，我们先从分类和流行病学入手，建立一个整体的框架。

你可能想问，为什么牙周炎要分这么多种？这得从分类体系的演变说起。早在1989年，分类主要看发病年龄和进展速度，把大部分牙周炎叫做“成人牙周炎”，认为它好发于35岁以上、进展缓慢；而另一组破坏迅猛、常在青春期前后发病的，则统称为“早发性牙周炎”，像青少年牙周炎、快速进展性牙周炎都属于这一类。但这种以年龄划线的方式有个大问题：临床上常见20多岁的年轻人出现类似成人牙周炎的慢性破坏，而有些成年人的牙周炎也会快速恶化。于是，1999年的新分类就彻底放弃了年龄命名，改成了“慢性牙周炎”和“侵袭性牙周炎”，并且把侵袭性牙周炎再分为局限型和广泛型。同时，还单独列出一类“反映全身疾病的牙周炎”，强调某些全身状况（比如糖尿病）会特殊地影响牙周破坏。到了2018年，分类更进一步，强调多维度分期和分级，诊断标准更具体：要求两个或以上不相邻牙的邻面附着丧失，或者≥2个牙的颊（舌）面有≥3mm的附着丧失，并且牙周袋深度>3mm。你看，每一次分类变革都反映了我们对牙周炎发病机制和个体易感性认识的加深。

搞清楚了分类脉络，我们来看看最常见的慢性牙周炎。它多见于成人，和明显的菌斑、牙石、牙龈炎症相伴。诊断的核心是牙周附着丧失和牙周袋形成，同时探诊会出血。根据2018年标准，牙周袋>3mm且有附着丧失就能诊断，但一定要排除单纯牙龈退缩这种没有炎症的情况。教材里把慢性牙周炎按严重程度分成三度：轻度时，牙周袋≤4mm，附着丧失1~2mm，X线显示牙槽骨吸收不超过根长的1/3；中度时，袋深≤6mm，附着丧失3~4mm，骨吸收在根长1/3到1/2之间，可能出现轻度牙松动和根分叉病变；重度就更严重了，袋深>6mm，附着丧失≥5mm，骨吸收超过根长1/2，牙齿明显松动，常伴根分叉病变和牙周脓肿。晚期还会继发很多问题，比如牙齿移位、食物嵌塞、𬌗创伤、根面龋、逆行性牙髓炎甚至口臭。这里要注意一个常见的误区：不能把牙周炎和牙龈炎混为一谈。牙龈炎只局限在牙龈软组织，没有附着丧失和骨破坏，是可逆的；而牙周炎已经累及牙槽骨和牙周膜，破坏不可逆。另外，不要以为只有老年人才得牙周炎，像下面要讲的侵袭性牙周炎就会发生在年轻人身上。

侵袭性牙周炎是另一类需要高度警惕的类型，它专门盯上全身健康的年轻人，破坏速度很快，而且有家族聚集性。它其实包含了过去的青少年牙周炎、快速进展性牙周炎等。根据患牙范围，局限型（LAgP）通常只累及第一恒磨牙和切牙，而广泛型（GAgP）则波及其他多数牙。这种病的一个典型特征是：口腔里的菌斑牙石量并不算多，但牙周破坏却相当严重，也就是我们说的“菌斑与破坏程度不匹配”。而且它进展迅速，更容易出现垂直型骨吸收。主要致病菌是伴放线聚集杆菌（Aa），此外牙龈卟啉单胞菌等也参与。部分患者还有中性粒细胞或单核细胞的趋化功能缺陷，这在非洲裔局限型患者中更明显，但不同种族结果不完全一致。流行病学上，黑人的患病率高于白人和亚洲人。所以，当你接诊一位年轻患者，口腔卫生看似还行，但牙周袋很深、附着丧失很重，就一定要警惕侵袭性牙周炎的可能。鉴别慢性还是侵袭性，关键看发病年龄、进展速度、菌斑量与破坏是否匹配，以及有无垂直骨吸收等特征。

最后，我们来看看牙周炎在人群中的分布情况。牙周病其实非常古老，我国新石器时代的颅骨上牙槽骨破坏发生率就达到了42.3%。现在全球范围内，龈炎和牙周炎都普遍流行。我国80年代的调查显示，中小学生龈炎患病率66.80%，15岁组高达80.46%；90年代的调查发现牙周炎患病率随年龄增长而升高，35~44岁组已相当可观。哪些因素会让你更容易得牙周炎呢？公认的危险因素包括：口腔卫生差（菌斑牙石多）、吸烟（包括被动吸烟）、年龄增大、男性、某些种族（如黑人青少年牙周炎患病率高）、社会经济状况低（高收入高教育程度者患病率低），还有糖尿病、代谢综合征等全身疾病，甚至某些基因多态性如IL-1基因也会增加易感性。一个著名的斯里兰卡茶场工人纵向研究发现，在没有口腔卫生措施的群体里，只有8%的人发展为快速进展性牙周炎，81%的人缓慢进展，11%的人病情稳定，这说明宿主本身的易感性在疾病进展中起了核心作用。所以，牙周炎并不是简单的“脏”出来的病，个体差异非常大。

到这里，我们已经把牙周炎的分类、主要类型的特点和流行病学轮廓都梳理了一遍。从分类演变到慢性、侵袭性牙周炎的鉴别，再到人群中的分布和危险因素，这些知识是后续深入学习病因和治疗的基础。如果你对某个类型还有疑问，可以随时问我；如果想马上检验一下掌握情况，可以做两道小测题；或者直接进入下一节，继续探讨牙周炎背后的病因和危险因素。
```

Tutor Step 1 Questions:

```json
[
  {
    "id": "q_section_1_1",
    "type": "single_choice",
    "stem": "关于慢性牙周炎的诊断，以下哪项是正确的？",
    "options": [
      "A. 只有牙周袋深度>5mm才能诊断慢性牙周炎",
      "B. 慢性牙周炎和侵袭性牙周炎在发病年龄上没有区别",
      "C. 只要有附着丧失和牙周袋形成，同时存在探诊出血，就可诊断牙周炎",
      "D. 慢性牙周炎不会出现牙齿松动"
    ],
    "answer": "C",
    "explanation": "根据讲义，诊断慢性牙周炎的核心指标是牙周附着丧失、牙周袋形成和探诊出血。2018年分类要求牙周袋>3mm且有附着丧失即可诊断，并非>5mm。慢性牙周炎与侵袭性牙周炎的发病年龄有明显区别（慢性多见于成人，侵袭性多见于年轻人）。晚期慢性牙周炎可出现牙齿松动。",
    "kg_node_ids": [
      "kg://牙周病学/010-牙周炎/core_1"
    ]
  },
  {
    "id": "q_section_1_2",
    "type": "single_choice",
    "stem": "侵袭性牙周炎区别于慢性牙周炎的一个典型特征是？",
    "options": [
      "A. 牙周袋深度一般较浅",
      "B. 菌斑牙石量与牙周破坏程度不匹配",
      "C. 只发生于老年人",
      "D. 通常不累及第一恒磨牙和切牙"
    ],
    "answer": "B",
    "explanation": "侵袭性牙周炎的一个显著特征就是菌斑牙石量与牙周破坏程度不匹配，即口腔卫生状况看似较好，但牙周破坏却很严重。A错误，侵袭性牙周炎通常牙周袋深。C错误，它多发于年轻人。D错误，局限型侵袭性牙周炎尤其好发于第一恒磨牙和切牙。",
    "kg_node_ids": [
      "kg://牙周病学/010-牙周炎/core_5"
    ]
  }
]
```

QA Answer:

```text
牙周炎之所以会导致牙槽骨吸收，根本原因在于细菌引发的宿主炎症反应。牙菌斑中的细菌（尤其是革兰阴性菌）释放脂多糖等产物，这些物质进入牙龈沟后，会刺激局部的免疫细胞和成纤维细胞释放一系列炎症介质，比如白介素-1（IL-1）、肿瘤坏死因子-α（TNF-α）和前列腺素E2（PGE2）。其中PGE2是牙周骨吸收最有力的刺激因素，它的水平在牙周炎患者中显著升高，并且与附着丧失程度直接相关。这些炎症介质共同作用，会诱导破骨细胞前体分化为成熟的破骨细胞，直接对牙槽骨进行“陷窝状”吸收，同时单核细胞辅助降解有机基质，最终导致牙槽骨高度降低。

除了慢性炎症这个最主要的原因，咬合创伤也能引起牙槽骨吸收，特别是在受压侧的牙槽骨出现垂直型骨吸收。不过，大量证据表明，即便没有明显的创伤，菌斑引起的炎症也足以造成骨破坏。在活动期的牙周炎中，可以观察到牙槽骨表面出现活跃的破骨细胞性骨吸收陷窝，这是牙齿支持组织丧失、牙齿松动甚至脱落的病理基础。好在牙槽骨的代谢很活跃，在远离炎症中心或治疗后，机体还能进行修复性再生，这也让牙周治疗有了生物学基础。
```

Tutor Step 2 Teaching Text:

```text
上一节我们了解了牙周炎的分类和流行病学基础，这一节继续来看看牙周炎为什么会发生，哪些因素会推着它往前走。

牙周炎的本质是细菌感染引发的慢性炎症。但并不是所有口腔细菌都会捣乱，关键在于龈下菌斑里那几株特定的致病菌。1996年世界牙周病研讨会确认了11种与牙周病密切相关的微生物，其中证据最充分的是伴放线聚集杆菌（简称Aa）、牙龈卟啉单胞菌（简称Pg）和福赛坦纳菌。它们通过分泌多种毒力因子直接破坏组织，或者激惹咱们的免疫系统造成炎性损伤。

这里要特别提一下伴放线聚集杆菌（Aa），它和局限型侵袭性牙周炎的关系非常紧密。Aa最厉害的武器是白细胞毒素，能直接杀伤白细胞。有趣的是，不同Aa菌株的毒力差别极大——有些坏家伙（比如JP2型）的白细胞毒素基因启动子区缺了一段530碱基对的片段，结果毒素产量暴增，还不管环境里氧多氧少都疯狂生产；而温和的菌株只能靠受氧调控的启动子缓慢产出一点点毒素。携带这种高毒力株的人，发生局限型侵袭性牙周炎的风险是低毒力株携带者的18倍。这说明，细菌自身的遗传变异对疾病发生的影响举足轻重。不过你可能会问，那是不是所有牙周炎都能找到Aa？其实并非如此。在中国、日本等亚洲国家，侵袭性牙周炎患者的Aa检出率明显低于欧美，而且查出来的多半是低毒力的c血清型，反而是Pg更常见。所以，我们不能简单地把某个致病菌的作用绝对化，各个地区的致病菌谱可能不同。这个认识也改变了治疗策略——除了机械清除菌斑，现在会联用甲硝唑、替硝唑这类药物来抑制龈下细菌。

说完了始动的细菌，我们再来看看宿主自身的危险因素。吸烟，绝对是牙周炎最明确的行为危险因素之一。烟草里那四千多种毒素，从多个层面加重牙周破坏。尼古丁会引起小血管收缩，牙龈血流一减少，局部就缺血缺氧，成纤维细胞的生长和胶原合成都受到抑制，牙周膜想修复都难。更麻烦的是，吸烟会按下免疫系统的暂停键——中性粒细胞、单核细胞的趋化和吞噬功能被直接抑制，血清里的IgG、IgM和分泌型IgA也降下来，口腔的抗菌防御整体变弱。有意思的是，吸烟者的牙龈常常不红不肿不易出血，龈沟液也减少，炎症“悄悄”进行，牙周组织却在默默崩塌。数据很直白：轻度吸烟者严重牙槽骨丧失的风险是非吸烟者的3.25倍，每天超过10支的重度吸烟者更是高达7.28倍，而且牙槽骨吸收程度只和吸烟量相关，跟牙面上有多少菌斑没关系。换句话说，吸烟让宿主对同样的菌斑产生更暴烈的破坏反应。更糟糕的是，吸烟还会让牙周治疗的效果打折，无论是非手术、手术还是再生治疗，都会受影响，维护期复发的风险也增高。所以，戒烟真的是牙周防治里绕不开的一环。

另一个躲不开的危险因素是糖尿病。它和牙周炎形影不离，具有双向关系——牙周炎也会反过来影响血糖控制。糖尿病患者得牙周炎的风险是非糖友的2到3倍。高血糖环境下，还原糖和蛋白质、脂质发生非酶反应，生成晚期糖基化终末产物（AGEs）。这些东西可不是善茬：一方面直接让胶原等结构蛋白变性，造成基底膜增厚、胶原合成减少、骨基质形成障碍，牙周组织代谢和修复都出问题；另一方面，AGEs与细胞表面的RAGE受体结合，激活信号通路，促使单核-巨噬细胞和内皮细胞大量释放TNF-α、IL-1β、IL-6这些炎症因子。这些因子本身就是强力的破骨细胞激活剂和胶原酶诱导剂，结果就是牙槽骨吸收和牙周膜破坏加剧。此外，AGEs还让中性粒细胞和单核细胞变得懒洋洋，趋化、吞噬功能受损，龈下致病菌就更容易扎下根，菌斑组成渐渐以革兰阴性菌为主，形成一个慢性感染灶，进一步激惹全身免疫。临床上能看到，血糖控制得好的患者，牙周炎的严重程度和发生率都会明显降低，但即使血糖控制理想，1型糖尿病患者的牙周炎风险仍然较高。另外，如果碰到牙周脓肿或重度牙周炎却找不到明确病因的患者，别忘了筛查一下糖尿病。

这一节我们聊了牙周炎的两大病因支柱——致病菌和危险因素，它们一个始动，一个推波助澜，共同导演了牙周组织的慢性破坏。下一节我们将会深入到组织破坏的具体机制，看看炎症反应如何一步步吃掉牙槽骨。有任何疑问随时可以问我，也可以做两道小测题自检一下，或者直接继续下一节。
```

Tutor Step 2 Questions:

```json
[
  {
    "id": "q_section_2_1",
    "type": "single_choice",
    "stem": "关于伴放线聚集杆菌（Aa）在牙周炎中的作用，以下哪项是正确的？",
    "options": [
      "A. Aa是所有类型牙周炎的主要致病菌，全球检出率一致",
      "B. 高毒力Aa菌株的白细胞毒素产量受氧浓度调控，低毒力株则不受",
      "C. 在中国等亚洲国家，侵袭性牙周炎患者中Aa的检出率明显低于欧美国家",
      "D. Aa产生的白细胞毒素主要作用是破坏牙龈上皮细胞"
    ],
    "answer": "C",
    "explanation": "根据讲义，我国等亚洲国家侵袭性牙周炎患者中Aa的检出率明显低于欧美国家，且多为低毒性c血清型，而Pg则相对多见。A选项错误因为Aa的作用有地域差异；B选项说反了，高毒力株不受氧浓度影响；D选项错误，白细胞毒素主要杀伤白细胞。",
    "kg_node_ids": [
      "kg://牙周病学/006-牙周病宿主的免疫炎症反应和促进因素/core_1"
    ]
  },
  {
    "id": "q_section_2_2",
    "type": "single_choice",
    "stem": "吸烟促进牙周炎进展的机制不包括以下哪项？",
    "options": [
      "A. 尼古丁引起牙龈小血管收缩，导致局部缺血",
      "B. 直接抑制中性粒细胞的趋化和吞噬功能",
      "C. 增加龈沟液量，使炎症反应更明显",
      "D. 减少血清IgG、IgM和分泌型IgA的水平"
    ],
    "answer": "C",
    "explanation": "讲义指出，吸烟者的牙龈炎症征象往往被掩盖，龈沟液量减少，而不是增加。A、B、D均为吸烟促进牙周炎的机制。",
    "kg_node_ids": [
      "kg://牙周病学/006-牙周病宿主的免疫炎症反应和促进因素/core_1"
    ]
  }
]
```

### 状态更新边界

```text
Tutor 记录 exposure，并可由 Memory 写入轻量 exposure / mastery signal。
QA 不推进课程，记录 confusion / reinforce signal，并可触发 profile / mastery 的保守更新。
Assessment 产生 grading / wrong-answer explanation / weak_kg_node_ids。
Orchestrator / Memory 确定性更新 mastery_by_kg_node。
Orchestrator 提交状态变更。
```

### 当前不足

- 旧文档里“完整 Master Agent 自主调度”口径已经降级：当前不需要强行复杂化。
- 后端按钮/输入框对应的 Orchestrator flow 已有最小 API smoke；前端页面还没接。
- 需要确保 Orchestrator 是产品流程入口，不是固定脚本冒充内容 Agent；内容仍由 Resource Factory / Tutor / QA / Assessment 的 LLM 能力生成。

### 下一步建议

下一步优先接前端最小体验页：显示 Tutor teaching_text 和 questions，输入框调用 QA，继续按钮调用 `/api/learning/continue`，小测提交调用 Assessment。更复杂的开放式调度后置，只有当动作空间明显变复杂时再考虑。

## 9. 当前不能声称已完成

目前不能包装成已完成的能力：

- 生产级课程 canonical handout 生成。
- 完整课程级 handout 稳定生成与复用。
- 从 handout 派生 slides / video / mindmap / quiz。
- Tutor 连续式真实讲解完全完成。
- QA 生产级多轮答疑与前端输入框闭环。
- Assessment 生产级支持所有题型、复杂病例分析和跨多节长期掌握度分析。
- 前端交互链路完整闭环。

Planner Agent 主路径可以说阶段性可用，但仍不能说完全完成；它还需要性能优化、更大主题集审计和证据绑定质量复核。

## 10. 交接文档更新规则

更新本文时必须遵守 [docs/DEVELOPMENT_DECISIONS_README_zh.md](./DEVELOPMENT_DECISIONS_README_zh.md) 的交接文档规则。

每次更新优先补：

```text
当前设计思路
现在做到哪一步
真实最终结果
当前不足
下一步推荐
不能声称完成的能力
```

禁止：

```text
只堆函数名
只贴 JSON / trace / timing
把中间产物当最终结果
把模型生成讲义人工润色后放进文档
说要补最终结果却只补过程日志
把 partial / fallback 包装成完成
```
