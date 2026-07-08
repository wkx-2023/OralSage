# OralSage 初始学习者测评 V1（9题最终版）

本文档定义 OralSage 当前阶段使用的首次学习前轻量测评方案。

适用目标：

- 用户第一次进入学习前完成轻量测评
- 生成初始画像字段
- 为学习计划推荐提供初始输入
- 为后续动态画像更新提供基线

配套文档：

- [docs/ORALSAGE_UNIFIED_DEVELOPMENT_SPEC_V1_zh.md](./ORALSAGE_UNIFIED_DEVELOPMENT_SPEC_V1_zh.md)
- [docs/ORALSAGE_FIELD_AND_VIEW_MODEL_SPEC_V1_zh.md](./ORALSAGE_FIELD_AND_VIEW_MODEL_SPEC_V1_zh.md)

## 1. 设计原则

- 题量：`9题`
- 题型：`单选`
- 首次学习前触发
- 允许跳过
- 允许在画像页重做
- 输出用于生成：
  - `profile_patch`
  - `style_dimensions`
  - `learner_type`
  - `learner_type_summary`
  - `score_breakdown`

说明：

- 本测评是产品 onboarding 测评，不是完整科研量表
- 维度设计参考学习策略、深层/表层学习、自我调节学习等权威研究思路
- 输出必须服务于 OralSage 的学习计划、讲课、测评和记忆更新

## 2. 测评输出目标字段

### 2.1 输出到 `profile_patch`

- `goal`
- `target_exam`
- `daily_available_minutes`
- `preferred_explanation`
- `pace`
- `assessment_preference`
- `planning_support_need`
- `confidence`

### 2.2 输出到 `style_dimensions`

- `explanation_depth`
- `entry_mode`
- `interaction_tendency`
- `pace`
- `planning_support_need`

### 2.3 额外输出

- `learner_type`
- `learner_type_summary`
- `score_breakdown`
- `assessment_trace`

## 3. Learner type 标签

`learner_type` 主要用于前端展示，不作为底层核心驱动字段。

推荐标签：

- `visual_structured_learner`
- `text_depth_learner`
- `case_driven_learner`
- `exam_oriented_learner`
- `practice_driven_learner`
- `steady_paced_learner`

## 4. 9 道题最终稿

### Q1

**question_id**: `LA_Q01_GOAL`

**题目**：你现在学习这个主题，最主要的目标是什么？

**选项**：

- A. 系统掌握这个知识点，建立完整理解
- B. 准备考试，抓重点和考点
- C. 结合病例应用，提升临床理解
- D. 查缺补漏，把薄弱部分补起来

**主要影响字段**：

- `goal`
- `style_dimensions.interaction_tendency`
- `learner_type`

**映射建议**：

- A -> `goal = course_learning`
- B -> `goal = exam_prep`
- C -> `goal = clinical_application`
- D -> `goal = gap_repair`

### Q2

**question_id**: `LA_Q02_DAILY_TIME`

**题目**：你每天一般愿意拿出多少时间来学习？

**选项**：

- A. 30 分钟左右
- B. 45 分钟左右
- C. 1 小时左右
- D. 2 小时左右

**主要影响字段**：

- `daily_available_minutes`
- `pace`
- `planning_support_need`

**固定映射**：

- A -> `daily_available_minutes = 30`
- B -> `daily_available_minutes = 45`
- C -> `daily_available_minutes = 60`
- D -> `daily_available_minutes = 120`

**辅助映射建议**：

- A -> `pace = slow`
- B -> `pace = standard`
- C -> `pace = standard`
- D -> `pace = fast`

### Q3

**question_id**: `LA_Q03_ENTRY_MODE`

**题目**：面对一个新知识点，你更希望系统怎么带你进入？

**选项**：

- A. 先给我一个病例或临床情境
- B. 先讲清楚基本概念和定义
- C. 先给我一道题，我做完再讲
- D. 先快速总结重点，再决定怎么展开

**主要影响字段**：

- `preferred_explanation`
- `style_dimensions.entry_mode`
- `learner_type`

**映射建议**：

- A -> `entry_mode.case_first += 1.0`
- B -> `entry_mode.concept_first += 1.0`
- C -> `entry_mode.question_first += 1.0`
- D -> `entry_mode.exam_point_first += 1.0`

**主字段建议**：

- A -> `preferred_explanation = case_first`
- B -> `preferred_explanation = concept_then_case`
- C -> `preferred_explanation = practice_first`
- D -> `preferred_explanation = exam_point_first`

### Q4

**question_id**: `LA_Q04_EXPLANATION_DEPTH`

**题目**：你更喜欢哪种讲解方式？

**选项**：

- A. 简洁一点，重点清楚就行
- B. 要讲明白机制和原理，我才安心
- C. 先讲重点，再根据需要展开
- D. 最好能结合病例来解释

**主要影响字段**：

- `preferred_explanation`
- `style_dimensions.explanation_depth`
- `style_dimensions.entry_mode`
- `learner_type`

**映射建议**：

- A -> `explanation_depth.concise += 1.0`
- B -> `explanation_depth.mechanism_deep += 1.0`
- C -> `explanation_depth.standard += 1.0`
- D -> `entry_mode.case_first += 0.5`
- D -> `explanation_depth.standard += 0.5`

### Q5

**question_id**: `LA_Q05_PACE`

**题目**：你更适合哪种学习节奏？

**选项**：

- A. 慢一点，一次学少一点但更扎实
- B. 中等节奏，正常推进
- C. 快一点，只要别太啰嗦
- D. 节奏可以快，但遇到难点要放慢

**主要影响字段**：

- `pace`
- `style_dimensions.pace`
- `planning_support_need`
- `learner_type`

**映射建议**：

- A -> `pace.slow += 1.0`
- B -> `pace.standard += 1.0`
- C -> `pace.fast += 1.0`
- D -> `pace.standard += 0.5`
- D -> `planning_support_need.normal += 0.5`

**主字段建议**：

- A -> `pace = slow`
- B -> `pace = standard`
- C -> `pace = fast`
- D -> `pace = standard`

### Q6

**question_id**: `LA_Q06_ASSESSMENT_PREFERENCE`

**题目**：你对学习过程中穿插小测的接受度如何？

**选项**：

- A. 可以经常测，学完一小节就测最好
- B. 偶尔测就行，不要太频繁
- C. 少测一点，尽量别总打断我
- D. 我更喜欢学完一段再统一测

**主要影响字段**：

- `assessment_preference`
- `style_dimensions.interaction_tendency`
- `learner_type`

**映射建议**：

- A -> `assessment_preference.frequent += 1.0`
- B -> `assessment_preference.moderate += 1.0`
- C -> `assessment_preference.light += 1.0`
- D -> `assessment_preference.light += 1.0`

**说明**：

- 这里影响的是 `section_quiz / daily_quiz`
- 不影响固定在 lesson 收尾时触发的 `stage_exam`

### Q7

**question_id**: `LA_Q07_PLANNING_SUPPORT`

**题目**：你希望系统在学习计划上提供多强的帮助？

**选项**：

- A. 希望系统帮我安排得很明确，我按计划走
- B. 可以给我推荐，但我想自己再调整
- C. 给我一个大方向就行，我自己安排
- D. 我先随便学学，之后再慢慢调整

**主要影响字段**：

- `planning_support_need`
- `style_dimensions.planning_support_need`
- `plan_adherence` 初始策略

**映射建议**：

- A -> `planning_support_need.high += 1.0`
- B -> `planning_support_need.normal += 1.0`
- C -> `planning_support_need.low += 1.0`
- D -> `planning_support_need.high += 0.5`

**主字段建议**：

- A -> `planning_support_need = high`
- B -> `planning_support_need = normal`
- C -> `planning_support_need = low`
- D -> `planning_support_need = high`

### Q8

**question_id**: `LA_Q08_PRIORITY_MODE`

**题目**：如果时间有限，你更希望系统优先帮你什么？

**选项**：

- A. 快速抓住考试重点
- B. 理清知识结构和逻辑
- C. 结合病例理解怎么应用
- D. 先补我最薄弱的地方

**主要影响字段**：

- `goal`
- `style_dimensions.explanation_depth`
- `style_dimensions.entry_mode`
- `learner_type`

**映射建议**：

- A -> `goal = exam_prep`
- B -> `goal = course_learning`
- C -> `goal = clinical_application`
- D -> `goal = gap_repair`

**辅助映射**：

- A -> `entry_mode.exam_point_first += 0.5`
- B -> `explanation_depth.mechanism_deep += 0.5`
- C -> `entry_mode.case_first += 0.5`
- D -> `planning_support_need.high += 0.5`

### Q9

**question_id**: `LA_Q09_AUTO_ADJUST`

**题目**：你希望系统根据你的学习表现自动调整计划吗？

**选项**：

- A. 希望，自动调整越多越好
- B. 可以调整，但最好先给我看推荐
- C. 可以少量调整，别改太多
- D. 不希望自动改，我更想自己控制

**主要影响字段**：

- `planning_support_need`
- `assessment_preference`
- `plan_adherence`
- `learner_type`

**映射建议**：

- A -> `planning_support_need.high += 1.0`
- B -> `planning_support_need.normal += 1.0`
- C -> `planning_support_need.low += 0.5`
- D -> `planning_support_need.low += 1.0`

## 5. `dimension_mapping` 设计建议

每道题都建议带显式映射关系。

建议结构：

```json
{
  "question_id": "LA_Q03_ENTRY_MODE",
  "dimension_mapping": {
    "A": {
      "style_dimensions": {
        "entry_mode": {
          "case_first": 1.0
        }
      },
      "profile_patch": {
        "preferred_explanation": "case_first"
      }
    },
    "B": {
      "style_dimensions": {
        "entry_mode": {
          "concept_first": 1.0
        }
      },
      "profile_patch": {
        "preferred_explanation": "concept_then_case"
      }
    }
  }
}
```

统一原则：

- 每题只影响 `1个主维度`
- 最多允许影响 `1个次维度`
- 主维度建议权重 `1.0`
- 次维度建议权重 `0.5`
- `goal` 和 `daily_available_minutes` 优先直接赋值

## 6. `score_breakdown` 最终建议

建议第一版统一输出：

```json
{
  "explanation_depth_score": {
    "concise": 0,
    "standard": 0,
    "mechanism_deep": 0
  },
  "entry_mode_score": {
    "concept_first": 0,
    "case_first": 0,
    "question_first": 0,
    "exam_point_first": 0
  },
  "pace_score": {
    "slow": 0,
    "standard": 0,
    "fast": 0
  },
  "assessment_preference_score": {
    "frequent": 0,
    "moderate": 0,
    "light": 0
  },
  "planning_support_need_score": {
    "low": 0,
    "normal": 0,
    "high": 0
  }
}
```

说明：

- 第一版不再保留 `resource_preference_score`
- 分数字段主要用于调试、解释和画像校准
- 不要求全部直接展示给最终用户

## 7. `learner_type` 生成建议

`learner_type` 不从单题直接得出，而是从多个维度综合推导。

### 7.1 推荐推导规则

#### `visual_structured_learner`

满足之一：

- `entry_mode = concept_first`
- 且用户偏好结构化、按步骤推进

#### `text_depth_learner`

满足：

- `explanation_depth = mechanism_deep`
- 且更偏系统理解而非速记

#### `case_driven_learner`

满足：

- `entry_mode = case_first`

#### `exam_oriented_learner`

满足：

- `goal = exam_prep`
- 且 `assessment_preference = frequent`
  或 `entry_mode = exam_point_first`

#### `practice_driven_learner`

满足：

- `entry_mode = question_first`
或
- `assessment_preference = frequent`

#### `steady_paced_learner`

满足：

- `pace = slow`
或
- `planning_support_need = high`

## 8. 最终输出结构建议

```json
{
  "profile_patch": {
    "goal": "course_learning",
    "target_exam": null,
    "daily_available_minutes": 45,
    "preferred_explanation": "case_first",
    "pace": "slow",
    "assessment_preference": "moderate",
    "planning_support_need": "normal",
    "confidence": 0.72
  },
  "style_dimensions": {
    "explanation_depth": {
      "dominant": "mechanism_deep",
      "status": "observed",
      "confidence": 0.68
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
      "confidence": 0.59
    }
  },
  "learner_type": "case_driven_learner",
  "learner_type_summary": "你更适合从病例或临床情境进入，再回到概念理解。",
  "score_breakdown": {},
  "assessment_trace": {
    "instrument_name": "learning_onboarding_v1",
    "instrument_version": "v1",
    "question_count": 9,
    "assessment_skipped": false
  }
}
```

## 9. 跳过与重做规则

### 9.1 跳过

- 首次测评允许跳过
- 跳过后使用默认画像字段
- 标记 `assessment_skipped = true`
- 计划按默认值生成

### 9.2 重做

- 允许在画像页重做
- 重做用于重新校准偏好类字段
- 不粗暴覆盖历史能力字段

## 10. 你接下来可直接开发的内容

基于本稿，你可以直接开始：

- 测评题目结构 JSON
- Learner Assessment 接口
- `question_id` / `dimension_mapping`
- 初始画像写入
- 学习计划推荐读取初始画像

---

本文件是 OralSage 初始学习者测评 V1 的最终题目设计稿。

如果以下任一项变化，必须同步更新本文档：

- 题目数量
- 题干或选项
- `question_id`
- `dimension_mapping`
- `score_breakdown`
- `learner_type` 推导规则
- 初始输出结构
