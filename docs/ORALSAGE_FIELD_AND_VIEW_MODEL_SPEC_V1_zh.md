# OralSage 字段与视图模型统一清单 V1

本文档用于统一 OralSage 当前阶段各模块共享字段、稳定枚举、资源 schema 和前端视图模型。

适用对象：

```text
学习者测试
用户画像
学习计划
讲课 / 小测 / 阶段卷
资源生成
微信机器人
前端页面
```

配套文档：

- [docs/ORALSAGE_UNIFIED_DEVELOPMENT_SPEC_V1_zh.md](./ORALSAGE_UNIFIED_DEVELOPMENT_SPEC_V1_zh.md)

## 1. 使用原则

### 1.1 稳定字段优先

前后端、微信机器人、未来总控 Agent 优先对接稳定字段，不直接耦合底层存储细节。

### 1.2 内部记忆与外部视图分离

- 记忆层内部：三元组 / 四元组式事实 + qualifiers
- 应用层对外：compact JSON 字段

### 1.3 共享字段先入文档再入代码

任何会被两个及以上模块共同依赖的字段，必须先进入本文档，再允许各模块直接写死依赖。

## 2. 稳定主标识

当前阶段统一主标识如下：

- `user_id`
- `session_id`
- `plan_item_id`
- `outline_node_id`
- `section_id`
- `slide_id`
- `segment_id`
- `question_id`
- `kg_node_id`
- `deck_id`
- `task_id`

说明：

- `outline_node_id` 是学习主链路中连接大纲、讲义 section、Tutor 讲稿、小测和 slide 的稳定主键
- `section_id` 在学习主链路中默认等同于当前 `outline_node_id`；新增资源不得引入与 `outline_node_id` 脱钩的第三套 section id
- `deck_id` 用于 PPT / slide 资源集合
- `task_id` 用于异步可选资源任务

## 3. 学习者测试输出字段

### 3.1 Learner Assessment 输入

建议输入对象：

```json
{
  "user_id": "demo",
  "instrument_name": "learning_onboarding_v1",
  "instrument_version": "v1",
  "answers": [],
  "daily_available_minutes": 40,
  "goal_topic": "牙周炎",
  "target_exam": "口腔执业医师"
}
```

字段说明：

- `instrument_name`
  - 当前量表 / 测评版本名
- `instrument_version`
  - 量表版本
- `answers`
  - 原始作答结果
- `daily_available_minutes`
  - 每日计划可投入分钟数
- `goal_topic`
  - 目标学习主题
- `target_exam`
  - 目标考试，可选

### 3.2 Learner Assessment 输出

建议输出对象：

```json
{
  "profile_patch": {},
  "style_dimensions": {},
  "raw_answers": [],
  "score_breakdown": {},
  "assessment_trace": {}
}
```

### 3.3 初始画像直接写入字段

首次学习前测评结束后，允许直接写入这些初始画像字段：

- `goal`
- `target_exam`
- `daily_available_minutes`
- `preferred_explanation`
- `pace`
- `assessment_preference`
- `planning_support_need`
- `confidence`

## 4. 用户画像稳定字段

### 4.1 Compact Profile

建议当前阶段对外稳定字段如下：

```json
{
  "level": "unknown",
  "goal": "course_learning",
  "preferred_explanation": "concept_then_case",
  "pace": "standard",
  "daily_available_minutes": 40,
  "target_exam": null,
  "assessment_preference": "moderate",
  "planning_support_need": "normal",
  "learner_type": "case_driven_learner",
  "learner_type_summary": "你更适合从病例或临床情境进入，再回到概念理解。",
  "known_weak_points": [],
  "mistake_patterns": [],
  "mastery_by_kg_node": {},
  "plan_adherence": 0.0,
  "review_need": "normal",
  "confidence": 0.2,
  "assessment_skipped": false,
  "last_assessed_at": null,
  "assessment_source": null
}
```

### 4.2 字段说明

- `level`
  - 当前学习基础水平
- `goal`
  - 当前学习目标
- `preferred_explanation`
  - 偏好的讲解方式
- `pace`
  - 偏好的学习节奏
- `daily_available_minutes`
  - 每日可投入时间
- `target_exam`
  - 目标考试，可选
- `assessment_preference`
  - 对小测频率的接受度
- `planning_support_need`
  - 对系统学习计划引导强度的需要
- `learner_type`
  - 前端展示用学习者标签
- `learner_type_summary`
  - learner_type 的解释文案
- `known_weak_points`
  - 当前已知薄弱点
- `mistake_patterns`
  - 当前稳定错因模式
- `mastery_by_kg_node`
  - 当前 KG node 掌握度摘要
- `plan_adherence`
  - 计划执行情况摘要
- `review_need`
  - 当前是否需要增加复习比重
- `confidence`
  - 画像置信度
- `assessment_skipped`
  - 是否跳过首次测评
- `last_assessed_at`
  - 最近一次测评时间
- `assessment_source`
  - 最近一次测评来源

### 4.3 推荐枚举

#### `preferred_explanation`

- `concept_then_case`
- `case_first`
- `exam_point_first`
- `practice_first`

#### `pace`

- `slow`
- `standard`
- `fast`

#### `assessment_preference`

- `frequent`
- `moderate`
- `light`

#### `planning_support_need`

- `low`
- `normal`
- `high`

#### `review_need`

- `low`
- `normal`
- `high`

## 5. 中间分析层字段

### 5.1 `style_dimensions`

`style_dimensions` 作为中间分析层存在，用于计划生成、讲解策略和资源偏好，不直接作为前端主字段。

建议结构：

```json
{
  "explanation_depth": {
    "dominant": "mechanism_deep",
    "status": "emerging",
    "confidence": 0.66
  },
  "entry_mode": {
    "dominant": "case_first",
    "status": "stable",
    "confidence": 0.81
  },
  "interaction_tendency": {
    "dominant": "asks_exam_points",
    "status": "observed",
    "confidence": 0.54
  },
  "pace": {
    "dominant": "slow",
    "status": "stable",
    "confidence": 0.72
  },
  "planning_support_need": {
    "dominant": "normal",
    "status": "observed",
    "confidence": 0.51
  }
}
```

### 5.2 建议维度

- `explanation_depth`
- `entry_mode`
- `interaction_tendency`
- `pace`
- `planning_support_need`

### 5.3 是否参与计划生成

参与。

主要用于：

- 决定每日任务数量
- 决定讲解深度
- 决定概念 / 病例进入顺序
- 决定 `section_quiz` 的频率
- 决定计划引导强度

## 6. 记忆层内部事实结构

建议统一结构：

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

必备字段：

- `subject`
- `predicate`
- `object`
- `confidence`
- `evidence_event_ids`
- `updated_at`

推荐 qualifiers：

- `kg_node_ids`
- `source_summary_ids`
- `reason`
- `score_delta`
- `severity_delta`

## 7. 学习计划字段清单

### 7.1 `plan_item` 最小结构

```json
{
  "plan_item_id": "plan_xxx",
  "user_id": "demo",
  "plan_date": "2026-07-08",
  "type": "learn_section",
  "title": "学习牙周炎第 1 小节",
  "notes": "",
  "status": "open",
  "source": "recommended",
  "recommendation_category": "continue_lesson",
  "linked_course_id": "lesson_xxx",
  "linked_course_title": "牙周炎系统学习",
  "kg_node_ids": ["kg://..."],
  "sort_order": 0,
  "deferred_count": 0,
  "completed_at": null
}
```

### 7.2 `plan_item.type`

- `learn_section`
- `review_section`
- `section_quiz`
- `daily_quiz`
- `stage_exam`
- `catch_up`
- `custom`

### 7.3 `plan_item.status`

- `open`
- `completed`

### 7.4 `plan_item.source`

- `manual`
- `recommended`
- `rollover`

### 7.5 `recommendation_category`

推荐结果内部可用：

- `continue_lesson`
- `review_weakness`
- `review_mastery`
- `catch_up`
- `practice`
- `review_summary`

说明：

- `plan_item.type` 是正式业务类型
- `recommendation_category` 是推荐来源 / 推荐逻辑标签

## 8. 小测与阶段卷字段

### 8.1 `section_quiz`

用途：

- 每小节后的即时小测
- 不等于阶段卷
- 可受 `assessment_preference` 影响频率

### 8.2 `daily_quiz`

用途：

- 当日或次日的集中巩固题
- 适合计划页和微信提醒场景

### 8.3 `stage_exam`

用途：

- 当前 lesson / topic unit 进入尾节时触发一次
- 不受 `assessment_preference` 影响

说明：

- lesson 总 section 数允许为 `4-8`
- `stage_exam` 覆盖当前 lesson 的完整学习单元
- 触发判据统一依赖：
  - `section_position.is_last_section = true`
  或
  - `current_index == total`

### 8.4 通用题目结构

建议最小字段：

```json
{
  "question_id": "q_001",
  "type": "single_choice",
  "section_id": "node_3",
  "kg_node_ids": ["kg://..."],
  "stem": "题干",
  "options": ["A. ...", "B. ...", "C. ...", "D. ..."],
  "answer": "B",
  "explanation": "解析"
}
```

### 8.5 `question.type`

建议先固定：

- `single_choice`
- `multiple_choice`
- `true_false`
- `short_answer`

当前阶段若实现未覆盖全部类型，前端仍按此枚举预留。

### 8.6 阶段卷结果结构

建议统一最小结构：

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

## 9. 资源生成字段清单

### 9.1 `handout_sections.json`

手册 / 讲义 canonical source。

当前代码中，单节讲义以 `session_id + outline_node_id` 缓存在 `handout_sections/{outline_node_id}.json`。`handout_section.content` 是 PPT、Tutor、QA、Assessment 的内容母本。

建议最小关心字段：

- `topic`
- `handout_status`
- `outline`
- `sections`
- `generation_summary`
- `warnings`

### 9.2 `section_status`

- `llm_generated`
- `failed`

### 9.3 `evidence_status`

#### handout / section

- `sufficient`
- `insufficient`
- `not_generated`

#### QA

- `sufficient`
- `partially_supported`
- `insufficient`

### 9.4 `handout_status`

`handout_status` 是目标聚合字段，用于描述一个 session / lesson 的整体讲义生成状态。当前代码的真实状态仍以每个 `handout_sections/{outline_node_id}.json` 内的 `section_status` 和 cache `status` 为准，`handout_status` 暂不作为已落库真源。

建议后续如需前端整体进度展示，可由所有小节状态计算：

- `generated`
  - 所有必需 handout section 已生成完成
- `partial`
  - 部分 handout section 已生成，仍有 pending / generating / failed / missing
- `failed`
  - 核心小节生成失败，无法形成可用讲义

### 9.5 `slides.json`

当前阶段 PPT 的直接控制对象是 `slides.json`。每张内容页必须绑定一个已有学习小节：

```text
slide.outline_node_id == slide.section_id == handout_section.outline_node_id == tutor_step.outline_node_id
```

默认粒度为**一节一页**：一个 outline node / handout section / tutor step 对应一张内容 slide。`slide_type` 只是前端模板类型，不代表固定生成顺序，也不要求每种类型都出现。

PPT 内容来源：

- `title` 优先来自 `handout_section.title`，可回退到 outline node title
- `bullets` / `content` 来自 `handout_section.content.overview`、`key_points`、`clinical_example`、`common_confusions`、`transition_to_next`
- `quiz` 如需展示，复用 `tutor_step.questions`，不由 PPT 重新生成
- 讲稿按 `outline_node_id` / `handout_section` 划分；当前实现中，每节讲稿保存在对应 `tutor_step.teaching_text`
- `narration_ref` 为可选同步引用；PPT 内容本身来自讲义，不从讲稿生成

建议最小结构：

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

### 9.6 `slide_type`

- `title`
- `objectives`
- `concept`
- `process`
- `comparison`
- `checklist`
- `summary`
- `quiz`

### 9.7 `theme_id`

- `clean_light`
- `clinical_blue`
- `warm_note`

### 9.8 `script_segments`

讲稿按 `outline_node_id` / `handout_section` 划分；当前实现中，每节讲稿保存在对应 `tutor_step.teaching_text`。第一版不得为了 PPT 重新定义或重写讲稿生成链路。

`script_segments` 仅作为 TTS、导出或前端同步所需的兼容视图：从已有 `tutor_step` 映射得到，而不是新的内容生成源。

当前实际讲稿真源仍是 `tutor_step.teaching_text`。后续如果做 TTS、字幕或跨端播放同步，可按 `outline_node_id` 将每节讲稿包装为 `script_segments`。

建议最小结构：

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

### 9.9 `audio_manifest`

建议最小结构：

```json
{
  "task_id": "audio_task_001",
  "deck_id": "deck_001",
  "segments": [
    {
      "segment_id": "script_seg_1",
      "slide_id": "slide_1",
      "audio_file": "audio/slide_1.mp3",
      "duration_seconds": 28
    }
  ]
}
```

### 9.10 `generated_resources.resource_type`

当前代码已经使用的实现枚举：

- `handout_md`
- `slides_json`
- `base_questions_json`
- `reading_list_json`
- `mindmap_json`
- `pptx_status_json`

这些名称偏向“文件/实现产物”，不能在没有兼容层的情况下直接替换，否则旧 DB 记录、smoke test 和按旧 `resource_type` 查询资源的代码可能失效。

未来资源页或对外业务展示可以使用更清晰的业务名称，但建议通过映射实现，而不是直接覆盖当前枚举：

| 当前实现枚举 | 业务展示 / 目标语义 |
| --- | --- |
| `handout_md` | `handout` |
| `slides_json` | `slides` |
| `mindmap_json` | `mindmap` |
| `pptx_status_json` | `pptx_status` |
| `base_questions_json` | `base_questions` |
| `reading_list_json` | `reading_list` |

目标业务资源名可包括：

- `handout`
- `mindmap`
- `slides`
- `pptx`
- `script`
- `audio`
- `stage_exam`

建议：当前阶段保留旧枚举作为存储真源；资源页或新 API 如需业务名，增加展示映射字段。

## 10. 任务与状态字段

### 10.1 可选资源任务字段

建议最小结构：

```json
{
  "task_id": "task_xxx",
  "resource_type": "pptx",
  "status": "queued",
  "session_id": "lesson_xxx",
  "deck_id": "deck_001",
  "download_path": null,
  "created_at": "2026-07-08T12:00:00Z",
  "updated_at": "2026-07-08T12:00:00Z"
}
```

### 10.2 任务状态

- `queued`
- `running`
- `generated`
- `failed`

### 10.3 Cache 状态

- `missing`
- `pending`
- `generating`
- `ready`
- `failed`

### 10.4 `learning_session.mode`

建议当前先保留：

- `course_learning`

后续如扩展，再补充更多 mode。

### 10.5 `learning_session.status`

建议当前先保留：

- `active`
- `completed`
- `abandoned`

说明：

- 当前代码未必全部写入这些值
- 规范先约定，避免后续不同模块自行发明状态名

## 11. 前端稳定视图模型

### 11.1 学习计划页

建议只依赖：

- `month_plan_view`
- `day_plan_view`
- `recommendation_preview`

#### `month_plan_view`

- `user_id`
- `year`
- `month`
- `days`
- `stats`

#### `day_plan_view`

- `date`
- `items`
- `stats`

#### `recommendation_preview`

- `plan_date`
- `recommendations`
- `trace`

### 11.2 用户画像页

建议只依赖：

- `compact_profile`
- `profile_state`
- `style_dimensions`
- `mastery_summary`
- `weakness_summary`

不直接渲染底层三元组事实。

### 11.3 学习主页面

建议当前依赖：

- `handout`
- `script_segments`
- `quiz`
- `mindmap`

### 11.4 资源页

建议当前依赖：

- `slides.json`
- `pptx`
- `audio_manifest`
- `stage_exam`

## 12. 微信机器人需要的字段

当前阶段只需稳定读取：

- `today_plan.items`
- `tomorrow_plan.items`
- `plan_item_id`
- `title`
- `type`
- `status`
- `learning_report.summary`

不需要直接读取：

- handout 原文
- 底层记忆事实
- 复杂资源 schema

## 13. 当前已定、尚未完全定的字段

### 13.1 已经可以直接用的

- `goal`
- `daily_available_minutes`
- `preferred_explanation`
- `pace`
- `assessment_preference`
- `planning_support_need`
- `plan_item.type`
- `plan_item.source`
- `recommendation_category`
- `question.type`
- `generated_resources.resource_type`

### 13.2 仍需你继续查量表后细化的

- `instrument_name`
- `instrument_version`
- `answers` 的具体题目结构
- `score_breakdown` 的细粒度含义
- `style_dimensions` 的最终分值计算方式
- `learner_type` 是否作为正式稳定字段保留

## 14. 给你同学最关键的字段

如果按你们当前分工，你同学最需要对齐的是：

- `slides.json`
- `slide_type`
- `theme_id`
- `script_segments`
- `audio_manifest`
- `generated_resources.resource_type`
- `plan_item.type`
- `recommendation_category`
- `today_plan / tomorrow_plan` 视图模型

也就是说，她除了看总规范文档，**最应该看这份字段清单的第 7、8、9、10、11、12 节**。

---

本文件是字段清单与视图模型清单，不替代总规范文档。

如以下内容变化，必须更新本文档：

- 初始画像正式字段
- `plan_item.type`
- `recommendation_category`
- 小测 / 阶段卷结构
- `slides.json` schema
- `script_segments` schema
- `audio_manifest` schema
- 微信机器人读取字段

## 15. 用户画像补充确认项（2026-07-08）

本节用于记录当前已经讨论确认、但前文尚未完全显式写死的画像相关约定。

### 15.1 `learner_type` 保留，但只作为展示标签

`learner_type` 可以保留，但不作为学习计划和资源生成的底层核心驱动字段。

统一原则：

- 真正驱动计划与资源生成的是：
  - `preferred_explanation`
  - `pace`
  - `planning_support_need`
  - `assessment_preference`
  - `daily_available_minutes`
  - `goal`
  - `style_dimensions`
- `learner_type` 和 `learner_type_summary` 主要用于前端展示与解释

建议理解为：

```text
style_dimensions / compact profile
-> 推导出 learner_type
-> 展示给用户
```

### 15.2 `planning_support_need` 纳入第一版

建议第一版纳入字段：

- `planning_support_need`

用途：

- 判断用户是否需要更强的计划引导
- 决定系统推荐的任务密度
- 决定是否要更明显地提醒和追踪计划执行

建议枚举：

- `low`
- `normal`
- `high`

### 15.3 初始画像字段补充

在原有 compact profile 基础上，建议补充这些字段：

- `learner_type`
- `learner_type_summary`
- `planning_support_need`
- `assessment_skipped`
- `last_assessed_at`
- `assessment_source`

建议最小示例：

```json
{
  "learner_type": "case_driven_learner",
  "learner_type_summary": "你更适合从病例或临床情境进入，再回到概念理解。",
  "planning_support_need": "normal",
  "assessment_skipped": false,
  "last_assessed_at": "2026-07-08T12:00:00Z",
  "assessment_source": "learning_onboarding_v1"
}
```

### 15.4 `learner_type` 建议标签

不建议把 `learner_type` 直接做成学习科学上争议较大的“视觉型/听觉型/动觉型”核心驱动标签。

建议你们采用“更贴近产品可操作逻辑”的标签，再允许前端文案里适度解释。

推荐 6 类展示标签：

- `visual_structured_learner`
- `text_depth_learner`
- `case_driven_learner`
- `exam_oriented_learner`
- `practice_driven_learner`
- `steady_paced_learner`

说明：

- 它们可以展示给用户看
- 但不直接替代 `style_dimensions`
- 如果你们后面确实想在文案里写“你更偏视觉化学习”，也应该作为描述语，不建议作为底层主标签

### 15.5 Learner Assessment 题型

当前建议：

- 题量：`10 题` 左右
- 上限：不超过 `15 题`
- 题型：`单选`

说明：

- 单选更适合 onboarding
- 更容易做稳定映射
- 更适合移动端和后续微信入口扩展

### 15.6 `answers` 结构建议

建议原始答案结构：

```json
[
  {
    "question_id": "LA_Q01_GOAL",
    "selected_option": "B"
  },
  {
    "question_id": "LA_Q02_DAILY_TIME",
    "selected_option": "C"
  }
]
```

说明：

- `question_id`：题目的稳定唯一编号
- `selected_option`：用户选择的选项标识

### 15.7 `question_id` 命名建议

建议格式：

```text
LA_Q01_GOAL
LA_Q02_DAILY_TIME
LA_Q03_ENTRY_MODE
LA_Q04_EXPLANATION_DEPTH
LA_Q05_PACE
LA_Q06_RESOURCE_PREFERENCE
LA_Q07_ASSESSMENT_PREFERENCE
LA_Q08_PLANNING_SUPPORT
...
```

其中：

- `LA` = Learner Assessment
- `Q01` = 题号
- 末尾 = 题目主维度

### 15.8 `dimension_mapping` 建议

每道题建议携带显式映射关系，说明：

- 选项影响哪些 `style_dimensions`
- 选项影响哪些 `profile_patch`

建议结构：

```json
{
  "question_id": "LA_Q03_ENTRY_MODE",
  "dimension_mapping": {
    "A": {
      "style_dimensions": {
        "entry_mode": {"case_first": 1.0}
      },
      "profile_patch": {
        "preferred_explanation": "case_first"
      }
    },
    "B": {
      "style_dimensions": {
        "entry_mode": {"concept_first": 1.0}
      },
      "profile_patch": {
        "preferred_explanation": "concept_then_case"
      }
    }
  }
}
```

### 15.9 `score_breakdown` 建议

建议第一版至少输出这些 breakdown：

- `explanation_depth_score`
- `entry_mode_score`
- `pace_score`
- `assessment_preference_score`
- `planning_support_need_score`

说明：

- 这些分数不是一定直接展示给最终用户
- 但它们有助于调试、解释和后续画像校准

### 15.10 测评跳过规则

当前已确认：

- 首次测评允许跳过

跳过后统一规则：

- 使用默认画像字段
- 记录 `assessment_skipped=true`
- 后续计划按默认值生成
- 前端可提示“完成初始测评可获得更精准推荐”

### 15.11 测评重做规则

当前已确认：

- 允许在画像页重做

重做后统一原则：

- 可以更新“偏好类字段”
  - `preferred_explanation`
  - `pace`
  - `assessment_preference`
  - `planning_support_need`
  - `learner_type`
- 不应粗暴覆盖“能力/历史类字段”
  - `known_weak_points`
  - `mistake_patterns`
  - `mastery_by_kg_node`
  - `plan_adherence`
  - `review_need`

也就是说：

```text
重做测评 = 重新校准偏好
不是清空历史学习画像
```

### 15.12 当前仍需你继续决定的内容

你后续看量表时，还需要继续补充：

- `learner_type_summary` 的文案风格
- 10 道题的最终题干
- 每题的选项文案
- 每题的 `dimension_mapping`
- `score_breakdown` 的最终加权细节
- 是否保留某些经典标签作为前端展示用副文案
