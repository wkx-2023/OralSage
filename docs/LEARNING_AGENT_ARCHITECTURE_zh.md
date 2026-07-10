# OralSage 学习 Agent 架构说明

本文档说明学习 Agent 后端架构、模块职责和当前运行流程。开发前还必须阅读：

- [docs/DEVELOPMENT_DECISIONS_README_zh.md](./DEVELOPMENT_DECISIONS_README_zh.md)
- [docs/LEARNING_AGENT_PROGRESS_AND_NEXT_ARCHITECTURE_zh.md](./LEARNING_AGENT_PROGRESS_AND_NEXT_ARCHITECTURE_zh.md)

## 1. 当前主架构

继续沿用项目原本模块名：

```text
Orchestrator
Planner
Resource Factory
Tutor
QA
Assessment
Memory
User Model
```

当前设计不是要改名字，也不是新增一套架构。只是把职责边界重新说清楚：

```text
Orchestrator 负责串起用户动作和状态变化。
Planner 负责课程大纲和证据绑定。
Resource Factory 负责生成 handout_sections，并在后续从 handout_sections 派生资源。
Tutor 负责当前小节讲解和 optional section quiz。
QA 负责当前学习上下文中的答疑。
Assessment 负责小测批改和错因 / weak KG node 输出。
Memory 负责 session、事件、答案和 mastery 状态保存。
```

课程页上的动作很明确：

```text
输入框提问 -> Orchestrator 调 QA
点击小测 -> Orchestrator 调 Assessment
点击继续 -> Orchestrator 推进到下一节并调 Tutor
```

所以当前不需要把 Orchestrator 包装成复杂开放式 Master Agent。它仍然叫 Orchestrator，只是当前阶段承担的是清晰的产品流程调度。

## 2. 正常运行过程

```text
1. 用户提出学习需求
   例如：“我想学牙周炎”

2. Planner 生成课程大纲
   输入：topic、用户状态、KG/KAG evidence
   输出：outline、outline_evidence_map、planning_trace
   LLM：决定学习单元、顺序和顺序理由
   Python：检索、喂证据、normalize、validate、构建 evidence map

3. Resource Factory 生成 canonical handout_sections
   输入：outline node、outline_evidence_map、证据原文
   输出：handout_sections
   LLM：判断证据需求并写讲义
   Python：读取证据、执行 targeted search、校验证据引用

4. Orchestrator 进入当前 section
   读取：current_outline_node、current handout_section、用户状态
   调用：Tutor
   输出：当前小节讲解、optional section quiz、可选下一步动作

5. 用户动作分流
   输入框提问 -> QA 答疑，不推进课程
   点击小测 -> Assessment 批改并产生 weak_kg_node_ids
   点击继续 -> Orchestrator 推进到下一节，再调用 Tutor

6. 状态更新
   Tutor 只产生 exposure signal，不直接改 mastery
   QA 只产生 confusion signal，不推进课程
   Assessment 产生 grading / weak_kg_node_ids
   Orchestrator / Memory 按 KG node 更新 mastery
```

关键原则：

```text
Outline 是学习路径和证据索引，不是正文来源。
handout_sections 是内容母本。
Tutor / QA / quiz / slides 必须基于 handout_sections 和证据。
mastery 按 KG node 更新，不按 handout key point 硬绑。
key point 可用于讲解和出题，但不一定等于 KG node。
```

## 3. 模块边界

### Orchestrator

负责：

```text
读取 session 当前状态
根据明确用户动作调用 Tutor / QA / Assessment / next
提交事件和状态变更
保证 QA 不推进课程
保证 continue / next 才推进 section
```

不负责：

```text
写教学正文
写答疑内容
写讲义
用关键词表冒充复杂智能调度
```

### Planner

负责：

```text
决定学什么
决定按什么顺序学
说明为什么这样排序
绑定 KG node / evidence chunk / 可选 edge、concept、question links
```

不负责：

```text
写讲义正文
直接给 Tutor / QA 当正文来源
替 Assessment 出题解释
```

### Resource Factory

负责：

```text
生成 canonical handout_sections
把每个 outline node 写成可教学的 handout section
保留 evidence_refs 和 missing_evidence
后续从 handout_sections 派生 slides / quiz / video / mindmap
```

不负责：

```text
重新规划课程顺序
用 Python 模板拼正文
把证据不足的内容硬写完整
绕过 handout_sections 直接从 outline 生成下游内容
```

### Tutor

负责：

```text
按当前 handout section 讲解当前小节
生成 1-2 道 optional section quiz
返回 available_user_actions
```

不负责：

```text
推进 current_outline_node_id
直接写 mastery
绕过 handout_section 根据 outline title 现场编讲
```

### QA

负责：

```text
回答用户在当前学习上下文中的问题
必要时做受控 targeted search
输出 related_kg_node_ids / confusion_kg_node_ids
```

不负责：

```text
推进课程进度
直接改 mastery
无限扩展成开放搜索引擎
```

### Assessment

负责：

```text
批改小测
给出 grading、explanation、weak_kg_node_ids
为 mastery 更新提供依据
```

不负责：

```text
直接和用户自由聊天
决定课程整体路径
直接替 Orchestrator 推进页面
```

### Memory

负责：

```text
保存 session
保存 outline / resources / events / answers
保存 mastery_by_kg_node
保证 QA 事件不改变 current_outline_node_id
```

## 4. 当前废弃或降级的口径

### 复杂开放式 Master Agent

当前不要写成“Master Agent 自主调度所有 Agent”。Orchestrator 仍然是主入口，但当前阶段主要承接明确用户动作。

### 旧 Resource Factory 主路径

“Resource Factory 直接根据 outline 生成 handout、slides、questions、mindmap”是废弃口径。当前应改为：

```text
Resource Factory 先生成 handout_sections
slides / quiz / video / mindmap 后续从 handout_sections 派生
```

### 固定脚本 Planner

固定医学维度只能 fallback / debug / smoke，不能作为 Planner 主路径。

### 直接从 outline 生成内容

Tutor、QA、quiz、slides 都不能直接根据 outline title 生成正文内容。

## 5. FastAPI 和脚本边界

FastAPI endpoint 只做接口层：

```text
backend/main.py
-> backend/services/learning/orchestrator.py
-> learning modules
```

脚本只用于 smoke、debug、验收展示，不能反向定义架构。

验收用户可见内容时，必须展示模型真实输出全文或完整任务单元，不能只写 smoke passed。

## 6. 当前优先级

当前最推荐顺序：

```text
1. 修 Resource Factory 中 handout_sections 生成稳定性和证据边界
2. 小修 Tutor 连续 section 语义和 teaching_text / questions 分离
3. 做 QA：current handout_section + evidence + controlled targeted search
4. 收拢 mastery 更新：按 KG node 更新
5. 再考虑 slides / mindmap / video / 更复杂的 Orchestrator 能力
```

不要先做复杂开放式 Master Agent，也不要先做一批以后会被 handout_sections 推翻的资源演示脚本。
