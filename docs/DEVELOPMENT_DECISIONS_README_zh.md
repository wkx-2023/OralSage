# OralSage 开发前必看：关键工程决策

本文档记录开发 OralSage 时必须遵守的关键工程决策。给 Claude Code、Codex 或其他开发 Agent 发任务前，必须先阅读本文件。

本文档适用于整个 OralSage 项目，尤其适用于：

```text
Agent
Planner
大纲
讲义
Tutor
QA
Assessment
Resource Factory
主 Agent 调度
任何 LLM 生成用户可见内容的任务
```

PPT 模板相关任务还必须先读：

```text
docs/PPT_LOCAL_DEVELOPMENT_GUIDE_zh.md
tools/ppt_preview/README_zh.md
tools/ppt_preview/templates/README_zh.md
```

其中：

```text
tools/ppt_preview/README_zh.md
```

说明旧 Python/规则预览沙盒与正式模板候选池的隔离边界。

```text
tools/ppt_preview/templates/README_zh.md
```

说明正式模板候选池、视觉风格修改准则、增加 page_type / 模板能力的准则。任何 PPT 模板修改任务都应优先参考这两个指南，不要从旧根预览器或已标记 reference-only 的模板开始扩展。

## 0. 总原则

```text
LLM 是主角。
Python 是配角。
```

LLM 负责：

```text
理解
规划
生成
教学表达
答疑
错因解释
内容组织
学习路径和顺序决策
```

Python 负责：

```text
喂料
检索
证据读取
格式归一化
结构校验
状态管理
guardrail
fallback 标记
运行 smoke / audit
```

Python 不能替 LLM 写正文、补医学内容、决定学习单元顺序、生成题目解释或把固定脚本包装成 Agent 能力。

## 1. LLM 固定结构输出规则

凡是让 LLM 输出固定结构，都必须走：

```text
LLM JSON mode
-> json.loads
-> normalize()
-> validate()
-> 通过后写入业务结果
-> 不通过则报错 / fallback / 重试 / 拆分调用
```

### normalize 只能修格式

允许：

```text
去掉 markdown 代码块
字段别名归一
字符串 trim
单字符串转单元素 list
忽略多余字段
轻微类型包裹修复
```

禁止：

```text
补章节
补正文
补标题
补证据 id
重排 LLM 给出的顺序
根据关键词改写业务内容
把缺失内容填默认值
```

### validate 只能校验

validate 负责检查：

```text
必填字段是否存在
标题 / 正文是否为空
证据 id 是否合法
JSON schema 是否符合业务契约
fallback / partial / insufficient 是否被正确标记
```

validate 不能替 LLM 修内容。校验不通过就失败、fallback、重试或拆分调用。

## 2. 禁止固定脚本冒充 Agent

所有核心 Agent 的主路径都必须保留真实 Agent 能力。固定规则只允许做：

```text
fallback
debug
smoke
validator
guardrail
最早期链路验证
```

固定规则不能承担：

```text
真实课程规划
真实讲义正文
真实 Tutor 讲解
真实 QA 答疑
真实错因解释
真实主 Agent 调度
```

如果某个实现以后要替换成 LLM 自主决策，改动范围应尽量局部。若以后需要改 3 个以上文件或推翻数据结构，通常说明当前方案是在制造过渡债务。

## 3. Planner 决策

Planner 主路径必须由 topic、用户状态和证据驱动，不允许只按固定医学教学维度生成大纲。

Planner 输出应包含：

```text
outline
outline_evidence_map
planning_trace
sequence_reason
kg_node_ids
evidence_chunk_ids
可选 kg_edge_ids / concept_ids / question_ids
```

Agent Planner 成功路径中，每个 node 必须至少有：

```text
kg_node_ids
evidence_chunk_ids
```

固定维度路径，例如“定义 / 病因 / 机制 / 表现 / 诊断 / 治疗”，只能作为 fallback/debug，不能作为主路径，也不能包装成 Agent 自主规划。

## 4. 内容母本决策

```text
Outline 不是正文来源。
handout_sections / canonical handout 才是内容母本。
```

正确链路：

```text
Planner
-> outline + outline_evidence_map
-> Resource Factory 生成 handout_sections
-> Tutor / QA / slides / video_script / mindmap / quiz 从 handout_sections 派生
```

禁止：

```text
讲义只根据 outline title 写正文
Tutor 只根据 outline 讲课
QA 只根据 outline 答疑
PPT / 视频 / 思维导图 / 题目直接从 outline 生成正文内容
Python 模板拼讲义正文
```

## 5. 讲义质量决策

讲义不是普通资源，而是后续 Tutor、QA、slides、quiz 的内容母本。

每个 key point 不应只是孤立摘要。证据支持时，应说明：

```text
这个小知识点是什么
为什么重要
它和本节学习目标的关系
它和前一个 / 下一个 key point 的关系
为什么讲完这个点后自然进入下一个点
如果是本节最后一个 key point，它如何收束本节并过渡到下一节
```

如果证据不足：

```text
不要硬写
不要编造
可以少写
必须标记 insufficient 或写入 missing_evidence
```

KG node / KG edge / concept 只能作为结构线索，不能单独扩写成医学结论。医学事实必须有 chunk 原文证据支撑。

## 6. 验收必须展示真实结果

所有涉及生成用户可见内容的任务，完成报告必须展示模型真实生成结果，不能只概括。

真实生成结果指：

```text
LLM / Agent 实际返回的内容
未经人工润色
未经总结改写
未经挑选性重写
保留原始章节、要点、解释、过渡、答疑、题目或资源正文
```

完成报告必须包含：

```text
实际运行命令
真实状态字段
是否 failed / partial / fallback / insufficient
真实生成内容全文或完整任务单元
是否人工润色，默认要求没有人工润色
```

讲义任务必须展示完整真实讲义正文，包括：

```text
overview
全部 key_points[].heading
全部 key_points[].explanation
clinical_example（如果有）
common_confusions（如果有）
transition_to_next
missing_evidence（如果有）
```

如果 builder 跑 2 节，就展示 2 节完整讲义。不能只写“内容更丰富”“smoke passed”“详见 JSON”。

## 7. 给开发 Agent 的任务必须写清楚

凡是涉及 Agent、Planner、大纲、讲义、Tutor、QA、Assessment、Resource Factory、主 Agent 调度的任务，任务说明必须包含：

```text
开发前必须阅读哪些文件
背景
终态形状：输入、输出、LLM 负责什么、Python 负责什么
本次实现范围
允许修改哪些文件
禁止修改哪些文件
具体要求
验收命令
完成报告必须包含什么
是否禁止 commit / push
```

并且必须明确：

```text
本步骤是否 fallback_rule_based
是否构建或消费 outline_evidence_map
是否使用 KG edges / concepts / question links / source_refs
哪些逻辑是 guardrail / validator / fallback
哪些能力仍未完成，不能包装成真实 Agent
如果生成用户可见内容，必须展示真实生成结果
```

## 8. 队友交接文档更新规则

`docs/LEARNING_AGENT_PROGRESS_AND_NEXT_ARCHITECTURE_zh.md` 是给人看的队友交接文档，不是代码流水账、函数索引、trace 仓库或 smoke 输出堆积区。

更新这个文档时，必须让下一个开发者不读代码也能快速看懂：

```text
当前设计思路是什么
现在做到哪一步
真实验收结果是什么
当前不足在哪里
下一步最推荐怎么做
哪些能力还不能声称完成
```

推荐写法：

```text
先讲模块边界和设计原因
再讲当前状态
再放真实最终结果样例
再讲不足和风险
最后给下一步推荐
```

允许写文件名和函数名，但只能作为定位辅助，不能成为文档主体。交接文档应少堆函数名，多讲清楚“为什么这样设计、输入是什么、输出是什么、LLM 负责什么、Python 负责什么”。

### 生成内容写入文档的规则

如果文档要记录讲义、Tutor、QA、Assessment、slides、quiz 等生成结果，必须放最终用户可见结果，不能把中间产物当最终结果。

必须遵守：

```text
展示模型真实返回内容
不人工润色
不总结改写
不补写缺失内容
不把 JSON / trace / 字段列表当作讲义示例
不把 partial / fallback 包装成完成
```

如果需要把 JSON 中的 `content` 字段整理成 Markdown，整理只能改变展示格式，不能改变原文措辞。

### 禁止的交接文档写法

禁止：

```text
只堆函数名，读者看不懂系统思路
只贴 JSON / trace / timing，不解释当前设计
说要补最终结果，却只补中间产物
把模型生成讲义人工润色后放进文档
把 smoke passed 当成内容质量通过
把还没完成的 Tutor / QA / Assessment 写成已完成
```

每次更新交接文档时，都要优先补“最终结果、当前不足、下一步建议”，而不是继续堆过程日志。

### 废弃流程的写法

废弃流程不要反复讲历史、旧架构细节或完整旧工作流。只需要在对应模块就地说明：

```text
xxx 是废弃流程
只能 fallback / debug / smoke（如果确实仍保留）
禁止用于主路径开发
禁止继续在它上面扩展能力
```

例如 Planner 的废弃流程写在 Planner 部分，讲义生成的废弃流程写在讲义部分，Tutor 的废弃流程写在 Tutor 部分。不要在文档里反复对比“旧架构怎么做、新架构怎么做”，重点写当前主路径和不能使用的边界。

## 9. 开发前自检清单

在提出方案、写代码、接受建议或验收完成报告前，必须自检：

1. LLM 拿到的是原始材料，还是 Python 组织过的半成品？
   如果是半成品，通常有问题。

2. LLM 承担的是完整决策，还是在预设结构上做修饰？
   如果只是修饰，通常有问题。

3. 如果删掉 Python 层，LLM 的输出还完整吗？
   如果不完整，说明 Python 抢了 LLM 的活。

4. 这个中间数据格式，在目标形态下还存在吗？
   如果不存在，就是过渡产物，不要做。

5. 如果以后换成 LLM 自主决策，改动范围多大？
   如果涉及 3 个以上文件或数据格式变更，通常是补丁式方案。

6. 稳定性方案是在解决 LLM 输出问题，还是绕过 LLM？
   如果是在绕过 LLM，打回。

7. normalize / validate 修的是格式，还是内容？
   如果修内容，越界。

8. 验收时用户能不能直接看到模型真实生成结果？
   如果只能看到概括、状态或“已通过”，验收不合格。

## 10. 禁止做法

- 不要把“固定顺序维度 + LLM 改标题”当作最终 Planner。
- 不要只输出漂亮大纲标题，却没有节点、边、概念、题目和原文索引。
- 不要让讲义 / Tutor / QA 只根据标题或 outline 生成内容。
- 不要让 PPT、视频、思维导图、题目、Tutor 或 QA 直接根据 outline 生成正文内容。
- 不要让 Python 决定大纲顺序、学习单元、内容分类、讲义正文、题目或答疑。
- 不要让 Python 生成半成品，再让 LLM 润色。
- 不要新增以后目标形态不存在的中间格式。
- 不要在 normalize / validate 里补业务内容。
- 不要把 rule_based fallback 说成 Agent 自主规划。
- 不要做后续必然推翻的演示脚本来冒充 MVP 能力。
- 不要让主 Agent 只靠固定关键词表调度，却声称是智能 Agent。
- 不要让 Tutor / QA / Assessment 长期只返回固定模板，却声称是真实教学、答疑或评估。
- 不要用“已生成 / 已优化 / smoke passed / 内容更完整”代替真实生成结果展示。
- 不要在完成报告里只概括生成内容；涉及用户可见内容时，必须把模型返回的真实结果直接展示给用户。
- 不要大篇幅复述已经废弃的旧架构或旧工作流；在对应模块标明“已废弃、禁止用于主路径开发”即可。
