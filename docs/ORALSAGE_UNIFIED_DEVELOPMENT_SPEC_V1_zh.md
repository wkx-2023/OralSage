# OralSage 项目统一开发规范总文档 V1

本文档用于统一 OralSage 当前阶段的产品目标、模块边界、数据结构、资源派生链、前后端契约和开发顺序。

适用范围：

```text
学习主链路
学习计划
学习画像 / 分层记忆
资源生成（讲义 / 讲稿 / PPT / 思维导图 / 语音）
阶段卷与补救
微信机器人提醒 / 打卡
未来总控 Agent 接入
```

相关文档：

- [docs/DEVELOPMENT_DECISIONS_README_zh.md](./DEVELOPMENT_DECISIONS_README_zh.md)
- [docs/LEARNING_AGENT_ARCHITECTURE_zh.md](./LEARNING_AGENT_ARCHITECTURE_zh.md)
- [docs/LEARNING_AGENT_PROGRESS_AND_NEXT_ARCHITECTURE_zh.md](./LEARNING_AGENT_PROGRESS_AND_NEXT_ARCHITECTURE_zh.md)

## 1. 项目定位

OralSage 是一个面向口腔医学学习的个性化学习系统。

底层依托教材 OCR、知识图谱、题库和权威资料构建 KAG 知识底座；上层通过学习路径规划、讲义生成、讲课、答疑、测评、学习计划和用户画像，形成：

```text
规划 -> 学习 -> 测评 -> 反馈 -> 再规划
```

的闭环学习系统。

当前阶段不把项目定义为“开放式万能聊天 Agent”，而是定义为“有明确学习流程和状态更新规则的学习系统”。

## 2. 当前阶段总原则

### 2.1 主链路优先于复杂调度

当前优先跑通以下链路：

- 讲课学习主线
- 学习计划主线
- 用户画像 / 记忆主线
- 学习资源派生主线

在这些链路全部稳定前，不优先开发复杂开放式总控 Agent。

### 2.2 讲义是资源生成的核心中间层

统一资源派生链：

```text
大纲 -> 讲义 -> 讲稿
讲义 -> 思维导图
讲义 -> slides.json -> 前端渲染 PPT / 可选导出 pptx
讲稿(按 outline_node_id / handout_section 划分，当前保存在 tutor_step.teaching_text) -> TTS 音频
```

注意：

- PPT 不直接从 outline 生成
- 思维导图不直接从 outline 生成
- 语音不直接从 handout 生成
- 讲稿按 `outline_node_id` / `handout_section` 划分；当前实现中，每节讲稿保存在对应 `tutor_step.teaching_text`，不得为了 PPT 重写讲稿生成链路
- `outline_node_id` 是大纲、讲义 section、Tutor 讲稿、小测和 slide 对齐的主键
- 所有资源均以 canonical handout 为核心中间层

### 2.3 记忆层与应用层分离

统一原则：

```text
记忆层内部：三元组 / 四元组式事实 + qualifiers
应用层对外：compact JSON 字段
```

也就是说，内部存储允许带：

- subject
- predicate
- object
- confidence
- evidence_event_ids
- kg_node_ids
- updated_at

但前端和 Planner / Tutor / Study Plan 优先读取 compact profile，不直接吃原始记忆事实。

## 3. 系统四条主线

### 3.1 讲课学习主线

```text
用户输入想学的主题
-> KAG 检索
-> Planner 生成大纲与 outline_evidence_map
-> Resource Factory 生成 handout_sections
-> Tutor 讲解当前 section
-> QA 处理中途提问
-> Assessment 处理答题
-> Memory / Mastery 更新
-> Continue 进入下一 section
```

### 3.2 学习计划主线

```text
用户手动输入计划 / 系统推荐计划
-> 用户采纳 / 修改 / 忽略
-> 计划写入 study_plan_items
-> 用户打卡完成 / 顺延 / 未完成
-> 晚间生成日报
-> 未完成任务迁移到次日
-> 反馈写入 learner memory
-> 影响次日推荐
```

### 3.3 用户画像 / 记忆主线

```text
L1 原始事件
-> L2 总结性事件 / summaries
-> L3 稳定画像字段 + compact profile
```

### 3.4 学习资源派生主线

```text
handout_sections
-> 讲稿
-> 思维导图
-> slides.json
-> pptx
-> TTS 音频
```

## 4. 已确认的关键产品决策

### 4.1 学习者测试先于计划生成与学习开始

在用户正式进入学习和计划生成之前，先进行轻量学习者测试。

测试目的：

- 判断学习者风格 / 学习人格倾向
- 获取每日可学习时长
- 获取目标学习内容 / 目标考试

测试结果用于生成初步用户画像，并作为学习计划推荐的初始输入。

当前阶段先约定接口，不在本文件中固定具体量表内容。后续可在查阅论文和权威量表后补全。

### 4.2 学习计划采用双模式

学习计划同时支持：

- 手动输入
- 系统推荐

原则：

- 用户手动输入优先
- 系统推荐为辅助
- 推荐项先 preview，再由用户选择采纳 / 修改后采纳 / 忽略

### 4.3 计划项最小单位与正式枚举

计划的最小业务单位为：

```text
plan_item
```

正式 `plan_item.type` 枚举定为：

- `learn_section`
- `review_section`
- `section_quiz`
- `daily_quiz`
- `stage_exam`
- `catch_up`
- `custom`

说明：

- `section_quiz`：每个小节后的即时小测
- `daily_quiz`：当天或次日的集中巩固题
- `stage_exam`：当前 lesson / topic unit 尾节触发的阶段卷
- `catch_up`：补上之前未完成 / 被顺延的学习任务

推荐逻辑内部允许保留单独的 `recommendation_category`，如：

- `continue_lesson`
- `review_weakness`
- `review_mastery`
- `catch_up`
- `practice`
- `review_summary`

但 `recommendation_category` 不替代正式 `plan_item.type`。

### 4.4 五小节固定触发阶段卷

规则固定：

```text
当前 lesson / topic unit 进入尾节 -> 触发 1 次 stage_exam
```

阶段卷结果必须输出：

- `overall_score`
- `section_scores`
- `weak_section_ids`
- `weak_kg_node_ids`

补救规则固定为：

- `< 60%`
  - 次日加入：`review_section + section_quiz`
- `60% <= score < 80%`
  - 次日加入：`review_section`
- `>= 80%`
  - 不强制补救，正常推进

补救优先级：

```text
weak_section_ids
-> weak_kg_node_ids
-> 若仍需补充，再加 daily_quiz
```

说明：

- 当前 lesson 的总 section 数允许为 `4-8`
- `stage_exam` 覆盖当前 lesson 的完整学习单元
- 触发判据统一依赖：
  - `section_position.is_last_section = true`
  - 或 `current_index == total`
- `stage_exam` 不受 `assessment_preference` 影响
- `section_quiz / daily_quiz` 才是可调节频率的测评形式

### 4.5 思维导图从讲义派生

两种导图均从讲义派生，不直接从大纲派生：

- 总学习目标思维导图：从完整讲义派生
- 已学内容思维导图：从已学讲义子集派生

### 4.6 原文证据本期前端不展示

本期原文证据不作为前端主要功能交付。

即：

- 原文与 span / page 引用继续在后台保留
- 当前阶段先不在主学习前端显式展示

### 4.7 微信这期只做提醒与打卡

本期不做完整小程序，不做开放式提问。

本期微信形态为：

```text
消息机器人 / 聊天机器人式入口
```

本期固定能力：

- 推送今日计划
- 推送明日计划
- 用户反馈完成情况
- 用户顺延任务
- 推送学习日报
- 查看帮助

### 4.8 未来总控 Agent 最后接入

总控 Agent 在当前阶段不负责每一步开放式自主决策。

未来总控 Agent 统一接管以下 4 个总入口：

- `plan_intake`
- `learning_intake`
- `checkin_intake`
- `daily_rollover`

当前学习主链路中的叶子级动作继续保留明确入口：

- `step`
- `interruption`
- `continue`
- `answer`

## 5. 学习画像与记忆规范

### 5.1 对外稳定字段

当前对外 compact profile 建议稳定展示 / 消费这些字段：

- `level`
- `goal`
- `preferred_explanation`
- `pace`
- `daily_available_minutes`
- `target_exam`
- `known_weak_points`
- `mistake_patterns`
- `mastery_by_kg_node`
- `confidence`

### 5.2 中间分析层

中间分析层保留：

```text
style_dimensions
```

它不直接作为前端主字段，而是作为分析层参与：

- 计划生成
- 讲解策略选择
- 题型比例选择
- 资源偏好选择

推荐的 `style_dimensions` 方向包括：

- `explanation_depth`
- `entry_mode`
- `interaction_tendency`
- `pace`
- `planning_support_need`

### 5.3 记忆层内部事实结构

统一成：

```json
{
  "subject": "user:demo",
  "predicate": "preferred_explanation",
  "object": "case_first",
  "confidence": 0.82,
  "evidence_event_ids": ["evt_xxx"],
  "kg_node_ids": ["kg://..."],
  "updated_at": "2026-07-08T12:00:00Z"
}
```

即：

- 核心是事实三元组
- 外层带 qualifiers

### 5.4 Learner Assessment 接口先约定

当前阶段先约定接口，不固定具体量表。

建议输入：

```text
LearnerAssessmentSubmission
```

字段建议：

- `user_id`
- `instrument_name`
- `instrument_version`
- `answers`
- `daily_available_minutes`
- `goal_topic`
- `target_exam` 可选

建议输出：

```text
LearnerAssessmentResult
```

字段建议：

- `profile_patch`
- `style_dimensions`
- `raw_answers`
- `score_breakdown`
- `assessment_trace`

## 6. 学习计划规范

### 6.1 计划项建议字段

当前数据库已有：

- `id`
- `user_id`
- `plan_date`
- `title`
- `notes`
- `status`
- `linked_course_id`
- `linked_course_title`
- `kg_node_ids`
- `sort_order`
- `deferred_count`

建议后续补充正式字段：

- `type`
- `recommendation_category`
- `source` (`manual` / `recommended` / `rollover`)

### 6.2 推荐规则（当前阶段）

初始推荐依据：

- 学习者测试得到的初始画像
- 每日可学习时长
- 目标学习内容

后续动态推荐依据：

- 当前未学完 section
- `known_weak_points`
- `mastery_by_kg_node`
- 阶段卷低分 section
- 错题 / 错因模式
- 未完成或被顺延的计划项

### 6.3 日报与次日迁移

晚间固定执行：

```text
汇总今日计划完成情况
-> 生成学习日报
-> 未完成项迁移至次日
-> 将反馈写入 learner memory
-> 影响次日推荐
```

## 7. 资源生成规范

### 7.1 讲义

讲义为 canonical 中间层。

统一交付物：

```text
handout_sections.json
```

### 7.2 讲稿

讲稿从讲义生成。

讲稿按 `outline_node_id` / `handout_section` 划分；当前实现中，每节讲稿保存在对应 `tutor_step.teaching_text`：

- 输入：当前 `outline_node` + `handout_section.content`
- 输出承载对象：`tutor_step`
- 核心字段：`outline_node_id`、`teaching_text`、`questions`、`quiz_scope`、`quiz_timing`、`signals`、`trace`
- 粒度：一个 `outline_node_id` / handout section 对应一节讲稿

现阶段不新增独立讲稿生成源。若 TTS、导出或前端同步需要 `script_segments`，应从已有 `tutor_step.teaching_text` 映射得到。

兼容视图建议结构：

```json
{
  "segment_id": "script_node_2",
  "slide_id": "slide_node_2",
  "outline_node_id": "node_2",
  "section_id": "node_2",
  "source": "tutor_step.teaching_text",
  "narration_text": "这里保存或引用 tutor_step.teaching_text 的内容……",
  "estimated_seconds": 28,
  "related_kg_node_ids": ["kg://..."]
}
```

### 7.3 TTS 音频

TTS = Text To Speech，文本转语音。

统一原则：

- 音频基于讲稿生成
- 音频按 `slide` 分段生成
- 每个 `slide` 对应一个音频片段

建议交付：

- `audio_manifest.json`
- `audio_files/...`

### 7.4 思维导图

思维导图从讲义派生。

建议结构化交付为：

```text
markmap_json / mindmap_json
```

### 7.5 PPT

PPT 是**可选任务**，不默认生成。

触发方式：

```text
用户主动选择“生成 PPT”
```

当前阶段统一策略：

- 优先生成 `slides.json`
- 前端基于 `slides.json` 渲染 PPT
- 如用户需要下载，再导出 `pptx`

统一生成链：

```text
讲义 -> slides.json -> 前端渲染 PPT
讲义 -> slides.json -> pptx
```

其中：

- `slides.json` 是中间规范格式
- 前端渲染 PPT 是当前阶段主展示方式
- `pptx` 是可选下载交付格式

建议 `slide_type` 先固定少量枚举，而不是无限自由模板：

- `title`
- `objectives`
- `concept`
- `process`
- `comparison`
- `checklist`
- `summary`
- `quiz`

建议 `theme_id` 先固定少量主题，而不是完全自由换肤：

- `clean_light`
- `clinical_blue`
- `warm_note`

建议 `slides.json` 最小字段如下：

```json
{
  "schema_version": "slides.v1",
  "deck_id": "deck_001",
  "session_id": "lesson_001",
  "deck_title": "牙周炎学习讲义",
  "theme_id": "clean_light",
  "slides": [
    {
      "slide_id": "slide_node_2",
      "outline_node_id": "node_2",
      "section_id": "node_2",
      "slide_type": "concept",
      "title": "牙周炎的定义",
      "subtitle": "",
      "bullets": ["定义", "核心特征", "临床意义"],
      "content": {
        "overview": "本节概述……",
        "key_points": [],
        "clinical_example": "",
        "common_confusions": [],
        "transition_to_next": ""
      },
      "tutor_step_ref": "node_2",
      "narration_ref": {
        "source": "tutor_step.teaching_text",
        "optional": true,
        "outline_node_id": "node_2"
      },
      "quiz_ref": {
        "source": "tutor_step.questions",
        "outline_node_id": "node_2"
      },
      "related_kg_node_ids": ["kg://..."],
      "visual_hint": {
        "diagram_type": "concept_card"
      }
    }
  ]
}
```

统一原则：

- 默认粒度为一节一页：一个 `outline_node_id` / `handout_section` / `tutor_step` 对应一张内容 slide
- `section_id` 在学习主链路中等同于 `outline_node_id`
- PPT 内容来自 `handout_section.content`
- 前端播放讲稿时可通过同一个 `outline_node_id` 绑定 `tutor_step.teaching_text`；`narration_ref` 是可选同步引用，不是 PPT 内容来源
- 小测展示复用 `tutor_step.questions`，PPT 不重新生成题目
- `slide_type` 是模板类型，不是固定页面顺序
- 不做无限自由 PPT 模板生成
- 先固定 schema
- 再由前端对不同 `slide_type` 做组件化渲染
- 如需下载，再由导出器将 `slides.json` 转成 `pptx`

## 8. 接口与契约规范

### 8.1 总原则

所有前后端、微信机器人、未来总控 Agent 的交互，必须优先遵守稳定接口契约。

统一原则：

```text
先定请求/响应 schema
再写业务逻辑
最后接前端或机器人
```

### 8.2 API 命名规范

- 所有 HTTP 接口统一挂在 `/api/...`
- 学习主链路统一挂在 `/api/learning/...`
- 使用 `snake_case` 字段命名
- 所有对外稳定字段必须先进入规范文档，再允许前后端直接依赖

### 8.3 响应包裹规范

统一响应结构建议保持为：

```json
{
  "schema_version": "v1",
  "ok": true,
  "module": "learning",
  "action": "study_plan_recommend",
  "result": {}
}
```

说明：

- `ok`：本次请求是否成功
- `module`：所属模块
- `action`：本次接口动作名
- `result`：真正业务结果

错误返回必须保证前端可稳定读取：

- `ok`
- `module`
- `action`
- `result.error` 或 `message`

### 8.4 稳定主标识

当前阶段统一主标识如下：

- `user_id`
- `session_id`
- `plan_item_id`
- `outline_node_id`
- `slide_id`
- `segment_id`
- `kg_node_id`

不要在不同模块中使用重复但语义不清的局部命名替代这些主标识。

### 8.5 学习计划接口契约

学习计划链路最少应保持这些动作：

- 查询月计划
- 查询日计划
- 手动创建计划
- 更新计划
- 完成计划
- 顺延计划
- 删除计划
- 推荐计划 preview
- 采纳推荐计划

其中：

- “采纳推荐计划”可以继续复用现有创建计划接口
- 推荐接口返回 preview，不直接落库

### 8.5.1 学习主链路正式入口

当前阶段学习主链路的正式稳定入口建议固定为：

- `POST /api/learning/session/start`
- `GET /api/learning/session/{session_id}/state`
- `POST /api/learning/route`
- `POST /api/learning/execute`
- `POST /api/learning/step`
- `POST /api/learning/interruption`
- `POST /api/learning/continue`
- `POST /api/learning/answer`

说明：

- `route / execute` 是总控风格入口
- `step / interruption / continue / answer` 是当前产品主路径的稳定快捷入口
- 未来总控接入时，优先复用 `route / execute`

### 8.5.2 /stream 接口定位

当前存在的 `/stream` 接口仅用于：

- 进度提示
- 伪流式 / SSE 用户体验增强

统一原则：

- `/stream` 不是业务唯一入口
- 前端、微信、未来总控不要只依赖 `/stream`
- 稳定业务能力必须始终有一个对应的非流式接口

### 8.6 Learner Assessment 接口契约

Learner Assessment 先只约定 schema，不固定量表内容。

接口至少需要支持：

- 提交测试答案
- 返回初始画像 patch
- 返回 `style_dimensions`
- 返回每日可学习时长
- 返回目标学习内容

### 8.7 PPT / 讲稿 / 音频接口契约

资源接口建议拆分成 3 层：

- 讲义生成接口
- PPT 生成接口
- 讲稿 / 音频生成接口

PPT 接口返回：

- `slides.json`
- `deck_meta`
- 可选 `pptx_path`

讲稿接口返回：

- 第一版直接返回或引用现有 `tutor_step.teaching_text`
- 如需 `script_segments`，仅作为从 `tutor_step` 映射出的兼容视图，不作为新的讲稿生成源
- `script_segments` 主要服务 TTS、字幕、导出或跨端播放同步；当前讲稿真源仍是按 `outline_node_id` 划分的 `tutor_step.teaching_text`

音频接口返回：

- `audio_manifest`
- `audio_files`

### 8.7.1 阶段卷接口契约

阶段卷链路必须统一题目 schema 和结果 schema。

建议题目最小结构：

```json
{
  "question_id": "stage_q_001",
  "type": "single_choice",
  "section_id": "node_3",
  "kg_node_ids": ["kg://..."],
  "stem": "题干",
  "options": ["A. ...", "B. ...", "C. ...", "D. ..."],
  "answer": "B",
  "explanation": "解析"
}
```

建议阶段卷结果最小结构：

```json
{
  "overall_score": 0.72,
  "section_scores": [
    {"section_id": "node_1", "score": 0.9},
    {"section_id": "node_2", "score": 0.4}
  ],
  "weak_section_ids": ["node_2"],
  "weak_kg_node_ids": ["kg://..."],
  "recommendation": {
    "action": "review",
    "target_sections": ["node_2"]
  }
}
```

说明：

- `section_quiz` 与 `stage_exam` 题目结构尽量统一
- 区别主要在题目来源、数量、覆盖范围和权重

### 8.8 微信机器人接口契约

微信机器人不直接接数据库，不直接拼业务逻辑。

它只调用后端稳定接口。

当前阶段固定动作建议映射为：

- 查询今日计划
- 查询明日计划
- 标记完成
- 标记顺延
- 查询学习日报

### 8.9 未来总控 Agent 契约

未来总控 Agent 不应直接依赖原始数据库表结构。

总控只通过稳定业务接口接管：

- `plan_intake`
- `learning_intake`
- `checkin_intake`
- `daily_rollover`

## 8.10 状态枚举与唯一写入者规范

### 8.10.1 统一原则

多人并行开发时，所有“状态字段”都必须满足：

- 明确枚举值
- 明确唯一写入者
- 前端只读稳定状态，不自行推断隐含状态

### 8.10.2 学习计划状态

- `plan_item.status`
  - `open`
  - `completed`

唯一写入者：

- `StudyPlan service`

说明：

- 顺延不新增单独状态
- 顺延通过 `plan_date` 更新 + `deferred_count` 增加体现

### 8.10.3 讲义状态

- `section_status`
  - `llm_generated`
  - `failed`

- `evidence_status`
  - `sufficient`
  - `insufficient`
  - `not_generated`

- `handout_status`
  - `generated`
  - `partial`
  - `failed`

唯一写入者：

- `Resource Factory`

说明：

- 当前代码真实状态以每个 `handout_sections/{outline_node_id}.json` 的 cache `status`、`section_status` 和 `evidence_status` 为准
- `handout_status` 是目标聚合字段，用于描述一个 session / lesson 的整体讲义生成进度，当前暂不作为已落库真源
- 如后续实现，可由所有小节状态计算：
  - `generated`：所有必需 handout section 已生成完成
  - `partial`：部分小节已生成，仍有 pending / generating / failed / missing
  - `failed`：核心小节生成失败，无法形成可用讲义

### 8.10.4 Tutor 状态

- `content_status`
  - `llm_generated`
  - `placeholder`
  - `failed`

唯一写入者：

- `Tutor`

### 8.10.5 QA 状态

- `evidence_status`
  - `sufficient`
  - `partially_supported`
  - `insufficient`

唯一写入者：

- `QA`

说明：

- QA 不推进课程进度
- QA 只负责回答与疑点信号输出

### 8.10.6 Assessment 状态

- `content_status`
  - `rule_based`
  - `llm_generated`

唯一写入者：

- `Assessment`

### 8.10.7 Cache 状态

建议统一理解缓存状态为：

- `missing`
- `pending`
- `generating`
- `ready`
- `failed`

说明：

- `ready` 不等于内容一定高质量
- 仍需结合 `section_status / content_status` 一起判断
- 前端不得伪装 cache hit
- cache miss / sync generation 需要明确暴露

### 8.10.8 课程进度唯一写入者

`current_outline_node_id` 和课程推进状态的唯一写入者是：

- `Orchestrator`

其他模块：

- `Tutor` 不推进进度
- `QA` 不推进进度
- `Assessment` 不直接推进进度，只给 recommendation

## 8.11 事件与回写规范

### 8.11.1 事件命名必须稳定

当前阶段建议稳定保留这些事件名：

- `lesson_started`
- `outline_completed`
- `outline_completion_failed`
- `tutor_step_generated`
- `tutor_exposure`
- `user_interruption_question`
- `answer_submitted`
- `progress_updated`
- `study_plan_item_created`
- `study_plan_item_completed`
- `study_plan_item_deferred`

### 8.11.2 surface 命名建议固定

建议固定这些 surface：

- `planning`
- `lecture`
- `qa`
- `quiz`
- `study_plan`
- `review`

### 8.11.3 唯一写入者

统一职责：

- `Orchestrator`
  - 写课程推进类事件
  - 写当前 outline node 变化

- `StudyPlan service`
  - 写计划创建 / 完成 / 顺延事件

- `Tutor`
  - 写 tutor 生成结果
  - 触发 exposure 信号

- `QA`
  - 写中途提问相关事件
  - 不写课程推进

- `Assessment`
  - 写答题结果
  - 输出补救 recommendation

## 8.12 状态真源（source of truth）规范

为避免多人并行时出现重复维护，统一真源如下：

- `user_profiles`
  - compact profile 真源

- `learner_memory`
  - L3 画像 / 记忆真源

- `study_plan_items`
  - 计划项真源

- `learning_sessions`
  - 学习 session 真源

- `lesson_outlines`
  - 课程大纲真源

- `generated_resources`
  - 结构化生成资源登记真源

前端不得自行缓存并长期覆盖这些真源。

## 9. 前端规范

### 8.1 当前阶段前端原则

当前阶段先统一：

- 页面级结构
- 接口契约
- 稳定展示层字段

当前阶段不强行先定最终视觉风格。

### 8.2 学习计划页

学习计划页允许实时联调与迭代。

原因：

- 输出结构相对简单
- 主要围绕已有后端接口展开
- 与微信打卡主线关系最近

### 8.3 用户画像页

用户画像页允许和字段确认同步推进，但必须遵守：

```text
前端优先对接稳定视图模型
不要直接绑定底层记忆存储结构
```

即：

- 读取 compact profile
- 读取 profile_state / style_dimensions 摘要
- 不直接原样渲染三元组记忆

### 8.4 资源展示页

当前阶段暂缓统一最终视觉方案，但需提前约定数据契约：

- 学习页：`handout + script + quiz + mindmap`
- 计划页：`plan_items + recommendations`
- 画像页：`compact_profile + learner_memory_summary`
- 资源导出页：`pptx / audio / stage_exam`

## 10. 微信机器人规范

### 9.1 本期目标

仅做：

- 计划提醒
- 打卡反馈
- 日报推送

### 9.2 建议固定指令

建议命令集：

- `今日计划`
- `明日计划`
- `完成 1`
- `顺延 1`
- `学习日报`
- `帮助`

数字表示当日计划项编号。

本期不支持开放自然语言改计划。

## 11. 未来总控 Agent 接入规范

未来总控 Agent 接管这些入口：

- `plan_intake`
- `learning_intake`
- `checkin_intake`
- `daily_rollover`

当前不接管：

- 每一步 Tutor 内容生成
- 每一步 QA 自由探索
- 每一步 Assessment 内部逻辑

这些仍由现有明确链路负责。

## 12. 当前推荐开发顺序（无周次）

### 第一优先级

- 学习计划双模式闭环
- 计划项类型正式化
- 阶段卷补救规则落地
- 用户画像稳定展示层

### 第二优先级

- Learner Assessment 接口与初始画像写入
- 思维导图从讲义派生
- 日报与次日迁移
- 微信提醒 / 打卡适配

### 第三优先级

- PPT 可选导出
- 讲稿 / slide 按 `outline_node_id` 对齐
- TTS 音频生成
- 未来总控 Agent 接入

## 13. 当前仍待补充但不阻塞开发的项

以下内容保留为后续补充项，不阻塞当前并行开发：

- 学习者测试的具体权威量表与题目内容
- `style_dimensions` 的最终细粒度定义
- PPT 的最终模板主题细节与视觉样式微调
- TTS 引擎的最终选型
- 微信机器人的最终承载平台与鉴权方式

## 14. 本期明确不做 / 后置的内容

本期明确后置：

- 原文证据前端展示
- 复杂开放式微信问答
- 自动动画
- 视频生成
- 高自由度总控 Agent
- 完整小程序形态

## 15. 补充协作规范（并行开发强相关）

本节用于补充那些不一定是“新功能”，但会直接影响多人并行开发的统一约定。

### 15.1 所有稳定 JSON / API 响应必须带 `schema_version`

适用对象：

- HTTP API 响应
- `slides.json`
- `mindmap_json`
- `script_segments`
- `audio_manifest`
- `stage_exam.json`

原则：

- 小改但不破坏旧字段，可保留同一主版本
- 字段含义变化、枚举变化、结构重排，必须提升版本

### 15.2 `/stream` 接口不是唯一业务入口

所有 `/stream` 接口只用于：

- 进度提示
- SSE / 伪流式体验增强

统一原则：

- `/stream` 不能成为唯一业务依赖
- 前端、微信机器人、未来总控必须优先依赖对应的非流式稳定接口

### 15.3 可选资源任务统一采用异步任务语义

适用对象：

- PPT 生成
- 音频生成
- 后续可选导出资源

统一原则：

```text
用户显式触发
-> 创建任务
-> 返回 task_id / resource_id
-> 轮询状态
-> 成功后返回访问或下载路径
```

建议最小字段：

- `task_id`
- `resource_type`
- `status`
- `session_id`
- `outline_node_id` 或 `deck_id`
- `created_at`
- `updated_at`
- `download_path` 可选

`resource_type` 的当前实现枚举和未来业务展示映射以 `ORALSAGE_FIELD_AND_VIEW_MODEL_SPEC_V1_zh.md` 第 9.10 节为准；不要在任务接口中另起一套枚举。

建议状态枚举：

- `queued`
- `running`
- `generated`
- `failed`

### 15.4 统一时区与日切逻辑

所有“按天”逻辑统一按：

```text
Asia/Shanghai
```

处理。

适用范围：

- 学习计划日期
- 今日 / 明日计划
- 每日晚报
- 未完成任务迁移
- 微信提醒时间

说明：

- 即使服务器部署在其他时区，也必须按 `Asia/Shanghai` 解释计划日期
- 前端和后端不得各自按本地时区独立计算“今天 / 明天”

### 15.5 资源输出目录需要统一

建议所有生成资源按 session 组织：

```text
data/learning/sessions/{session_id}/
```

建议子目录包括：

- `handout_sections/`
- `slides/`
- `ppt/`
- `scripts/`
- `audio/`
- `mindmaps/`
- `exams/`

建议文件命名包含：

- `session_id`
- `outline_node_id` 或 `deck_id`
- `schema_version`

### 15.6 阶段卷结果 schema 需要全项目统一

建议统一最小结果结构：

```json
{
  "overall_score": 0.72,
  "section_scores": [
    {"section_id": "node_1", "score": 0.9},
    {"section_id": "node_2", "score": 0.4}
  ],
  "weak_section_ids": ["node_2"],
  "weak_kg_node_ids": ["kg://..."],
  "recommendation": {
    "action": "review",
    "target_sections": ["node_2"]
  }
}
```

目的：

- 方便计划模块直接读取补救目标
- 方便前端统一渲染阶段卷结果
- 方便微信日报总结调用

## 16. Learner Assessment 与初始画像补充决策

本节用于补充当前已经确认、但需要在总规范层面显式保留的 Learner Assessment 与画像决策。

### 16.1 初始测评允许跳过

当前已确认：

- 首次学习前允许跳过测评

跳过后规则：

- 使用默认画像字段
- 记录 `assessment_skipped=true`
- 学习计划按默认值生成
- 前端可提示“完成初始测评可获得更精准推荐”

### 16.2 允许在画像页重做测评

当前已确认：

- 允许用户在画像页重做 Learner Assessment

重做后统一原则：

- 用于重新校准“偏好类字段”
- 不粗暴覆盖“能力 / 历史类字段”

可更新字段包括：

- `preferred_explanation`
- `pace`
- `assessment_preference`
- `planning_support_need`
- `learner_type`

不应直接清空或覆盖：

- `known_weak_points`
- `mistake_patterns`
- `mastery_by_kg_node`
- `plan_adherence`
- `review_need`

### 16.3 `learner_type` 保留，但只作为展示标签

当前已确认：

- 保留 `learner_type`
- 允许前端展示给用户
- 不作为底层核心计划驱动字段

统一原则：

- 真正驱动计划和资源生成的是 `style_dimensions + compact profile`
- `learner_type` 是对这些结果的解释性标签

### 16.4 `planning_support_need` 纳入第一版

当前已确认：

- 第一版纳入 `planning_support_need`

用途：

- 判断用户是否需要更强的计划引导
- 影响计划任务密度
- 影响提醒与跟进强度

建议枚举：

- `low`
- `normal`
- `high`

### 16.5 初始测评推荐题型

当前已确认建议：

- 题量控制在 `10 题` 左右
- 上限不超过 `15 题`
- 题型采用 `单选`

原则：

- 轻量
- 易映射
- 适合首次 onboarding

### 16.6 小测偏好字段解释

`assessment_preference` 指的是：

- 用户对后续学习过程里 `section_quiz / daily_quiz` 频率的接受度

不影响：

- 当前 lesson 尾节 / 收尾阶段触发的 `stage_exam`

也就是说：

- `stage_exam` 是系统固定机制
- `section_quiz / daily_quiz` 才是可调教学策略

### 16.7 量表参考方向

当前建议：

- 参考 `MSLQ` 思路
- 参考 `R-SPQ-2F` 思路
- 参考 `ASSIST` 思路

统一原则：

- 借用权威量表的维度设计思想
- 不直接照搬完整长量表
- 输出必须服务于 OralSage 的计划、讲课和资源生成

---

本文件为 OralSage 当前阶段统一开发规范 V1。

当以下任一项发生变化时，必须同步更新本文档：

- 计划项正式枚举变化
- 阶段卷补救规则变化
- Learner Assessment 输出接口变化
- 资源派生链变化
- 微信机器人固定能力变化
- 总控 Agent 接管入口变化
