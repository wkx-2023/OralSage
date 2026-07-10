# PPT 本地开发指南

本文档只服务 PPT 生成功能的本地开发，不替代对外协作规范。对外稳定口径以：

- `docs/ORALSAGE_FIELD_AND_VIEW_MODEL_SPEC_V1_zh.md`
- `docs/ORALSAGE_UNIFIED_DEVELOPMENT_SPEC_V1_zh.md`

为准。

模板修改入口文档：

- `tools/ppt_preview/templates/README_zh.md`

当前正式模板候选池只以 `tools/ppt_preview/templates/README_zh.md` 为入口。如果只是改变视觉风格，和如果要增加新的模板能力 / page_type，应按该文档分开处理，不要混在同一轮任务里。

注意：`tools/ppt_preview/index.html`、`tools/ppt_preview/js/`、`tools/ppt_preview/styles/`、`sample-slides.v1.json`、`stress-slides.v1.json` 这一套是早期规则预览沙盒，不属于正式提交候选池。新的模板任务、LLM 契约和验收文档不要再指向它；正式模板开发只从 `tools/ppt_preview/templates/` 下的 active 模板进入。

## 1. 本阶段目标

本阶段核心目标是跑通 PPT 生成链路骨架，而不是一次性完成所有精细母版设计：

```text
handout_section/{outline_node_id}.json
  -> slide/{outline_node_id}.json
  -> slides/slides.v1.json
  -> preview / renderer
```

其中：

- `handout_section` 是单节讲义，是 PPT 主体内容母本。
- `slide/{outline_node_id}.json` 是单节 PPT 内容页缓存。
- `slides/slides.v1.json` 是课程级聚合视图，由已有单节 slide 按 outline 顺序拼成。
- renderer 根据 `layout_plan` 把 slide 渲染成 16:9 课件页面。

本阶段不追求完整精修母版，但必须具备基础 PPT 排版逻辑：固定画布、标题区、主内容区、辅助区、页脚、长文本保护和空模块不占位。`pptx` 下载、TTS 音频、复杂动画和最终视觉母版都后置。

## 2. 不改动的边界

第一版 PPT 开发不得改坏现有讲义和讲稿链路：

- 不重写 Tutor 讲稿生成逻辑。
- 不把讲稿改成按 PPT 自由切页。
- 不让 PPT 重新生成小测题。
- 不让 PPT 直接根据 outline title 编正文内容。
- 不改变课程推进逻辑和 `current_outline_node_id`。
- 不要求全课所有节点完成后才生成 PPT。
- 不把预览器固定模板效果等同于自动 PPT 生成效果。

讲稿仍然按 `outline_node_id` / `handout_section` 划分。当前实现中，每节讲稿保存在对应的 `tutor_step.teaching_text`。PPT 可以通过 `narration_ref` / `quiz_ref` 引用 `tutor_step`，但 PPT 主体内容不依赖讲稿先生成。

## 3. 当前已有对象

### 3.1 大纲节点

来源：Planner / session outline。

关键字段：

```json
{
  "id": "node_2",
  "title": "牙周炎的定义",
  "learning_objectives": [],
  "sequence_reason": "",
  "kg_node_ids": []
}
```

说明：

- `id` 是后续统一使用的 `outline_node_id`。
- 它控制课程小节划分。
- PPT 不能只靠 outline title 生成正文。

### 3.2 讲义小节 `handout_section`

来源：`generate_handout_section()`，缓存路径为：

```text
data/learning/sessions/{session_id}/handout_sections/{outline_node_id}.json
```

关键字段：

```json
{
  "outline_node_id": "node_2",
  "title": "牙周炎的定义",
  "section_status": "llm_generated",
  "evidence_status": "sufficient",
  "used_evidence": {
    "kg_node_ids": [],
    "kg_edge_ids": [],
    "concept_ids": [],
    "question_ids": [],
    "evidence_chunk_ids": []
  },
  "content": {
    "overview": "",
    "key_points": [
      {
        "heading": "",
        "explanation": "",
        "evidence_refs": []
      }
    ],
    "clinical_example": "",
    "common_confusions": [],
    "transition_to_next": ""
  },
  "missing_evidence": []
}
```

用途：

- PPT 内容母本。
- `title` 生成 slide 标题。
- `overview`、`key_points`、`clinical_example`、`common_confusions` 生成 slide 内容。
- `transition_to_next` 可以保留在上游讲义中，但不进入 PPT 输出。
- `used_evidence.kg_node_ids` 生成 `related_kg_node_ids`。

### 3.3 讲稿小节 `tutor_step`

来源：`build_tutor_step()` / `next_step()`，缓存路径为：

```text
data/learning/sessions/{session_id}/tutor_steps/{outline_node_id}.json
```

关键字段：

```json
{
  "outline_node_id": "node_2",
  "teaching_text": "这一节的讲稿正文……",
  "questions": [],
  "quiz_scope": "section",
  "quiz_timing": "after_section",
  "available_user_actions": ["ask_question", "take_quiz", "continue"],
  "expected_user_action": "choose_next_action",
  "signals": {
    "used_kg_node_ids": [],
    "used_handout_section_title": ""
  },
  "trace": {
    "uses_handout_section": true
  },
  "content_status": "llm_generated"
}
```

用途：

- 前端播放或展示讲稿时读取 `teaching_text`。
- PPT 小测如需展示，复用 `questions`。
- PPT 不重新生成讲稿，也不重新生成题目。

## 4. 统一绑定关系

所有 PPT 内容页必须满足：

```text
slide.outline_node_id == slide.section_id == handout_section.outline_node_id
```

如果引用讲稿或小测，则同时满足：

```text
slide.outline_node_id == tutor_step.outline_node_id
```

第一版默认一小节一页：

```text
一个 outline node / handout_section -> 一张内容 slide
```

讲稿和 PPT 是从同一个 `handout_section` 并行派生：

```text
outline node -> handout_section -> tutor_step
outline node -> handout_section -> slide
```

不要实现成：

```text
handout_section -> tutor_step -> slide
```

封面页可以没有 `outline_node_id`，但普通内容页必须有。

## 4.1 单节生成与聚合

由于当前学习链路存在“先生成第一节，后续节点后台慢慢补齐”的设定，PPT 也必须按单节生成和缓存。

推荐缓存路径：

```text
data/learning/sessions/{session_id}/slides/{outline_node_id}.json
```

课程级聚合路径：

```text
data/learning/sessions/{session_id}/slides/slides.v1.json
```

生成规则：

- 某个 node 的 `handout_section` ready 后，即可生成该 node 的 slide。
- 不等待全课所有 `handout_section` 完成。
- 不等待 `tutor_step` 先生成；如果讲稿未 ready，可保留 `narration_ref` 并记录 warning。
- 聚合器只把已有单节 slide 按 outline 顺序拼成 deck。
- 缺失节点不阻塞已有 slide，写入 warnings。

## 5. slides.json 第一版 schema

建议输出：

```json
{
  "schema_version": "slides.v1",
  "deck_id": "deck_{session_id}",
  "session_id": "lesson_001",
  "deck_title": "牙周炎学习讲义",
  "theme_id": "clean_light",
  "source": {
    "type": "handout_sections+tutor_steps",
    "granularity": "one_slide_per_outline_node"
  },
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
        "overview": "",
        "key_points": [],
        "clinical_example": "",
        "common_confusions": []
      },
      "layout_plan": {
        "template_id": "standard_learning",
        "density": "medium",
        "regions": {
          "intro": ["block_node_2_01"],
          "main": ["block_node_2_02"],
          "support": [],
          "callout": []
        }
      },
      "tutor_step_ref": "node_2",
      "narration_ref": {
        "source": "tutor_step.teaching_text",
        "outline_node_id": "node_2"
      },
      "quiz_ref": {
        "source": "tutor_step.questions",
        "outline_node_id": "node_2"
      },
      "related_kg_node_ids": [],
      "visual_hint": {
        "diagram_type": "concept_card"
      }
    }
  ],
  "warnings": []
}
```

## 6. 字段生成规则

### 6.1 deck 字段

- `schema_version`: 固定为 `slides.v1`。
- `deck_id`: 可以先用 `deck_{session_id}`。
- `session_id`: 当前学习 session id。
- `deck_title`: 优先使用 outline title 或 session topic。
- `theme_id`: 第一版固定 `clean_light`。

### 6.2 slide 标识

- `slide_id`: `slide_{outline_node_id}`。
- `outline_node_id`: 来自大纲节点 `id`。
- `section_id`: 第一版等同于 `outline_node_id`。

### 6.3 slide 标题

优先级：

1. `handout_section.title`
2. outline node `title`
3. `未命名小节`

### 6.4 bullets

第一版建议：

- 从 `handout_section.content.key_points[*].heading` 生成。
- 最多取 3-5 条。
- 如果没有 key points，可回退到 `overview` 摘要句。
- 不要直接把很长的 `explanation` 塞进 bullets。

### 6.5 content

保留 PPT 展示层需要的结构化内容，方便 renderer / 模板渲染：

- `overview`
- `key_points`
- `clinical_example`
- `common_confusions`

不进入 PPT 输出：

- `transition_to_next`

说明：

- `slides.v1.content` 是 PPT 展示层内容投影，不是 `handout_section.content` 的完整原样镜像。
- `clinical_example` 和 `common_confusions` 是可选增强模块，没有内容时不生成对应展示 block。
- `key_points[*].heading` 适合做 bullet / 卡片标题。
- `key_points[*].explanation` 是核心讲义内容，PPT 展示时需要截断、压缩或生成 display text，不能无脑铺满页面。

### 6.5.1 layout_plan

`layout_plan` 是内容块到 PPT 区域的中间层。renderer 不应该只按 `blocks` 顺序从上到下堆叠，而应按 `layout_plan.regions` 放置内容。

第一版规则 planner 可以先输出：

```json
{
  "template_id": "standard_learning",
  "density": "medium",
  "regions": {
    "intro": ["block_node_1_01"],
    "main": ["block_node_1_02", "block_node_1_03"],
    "support": ["block_node_1_05"],
    "callout": ["block_node_1_06"]
  }
}
```

基础规则：

- `overview` -> `intro`
- `key_point` -> `main`
- `clinical_example` -> `support`
- `confusion` -> `callout`
- 空模块不占位
- key points 较多或正文较长时 `density = "dense"`

后续如接 LLM，LLM 也必须输出同一类 `layout_plan`，不能直接写 HTML/CSS，也不能新增医学内容。

### 6.6 narration_ref

第一版只引用，不复制全文也可以：

```json
{
  "source": "tutor_step.teaching_text",
  "outline_node_id": "node_2"
}
```

如果前端需要一次性拿到全文，可以额外加：

```json
{
  "narration_text": "..."
}
```

但要记住它只是 `tutor_step.teaching_text` 的拷贝，不是新的生成源。

### 6.7 quiz_ref

小测引用：

```json
{
  "source": "tutor_step.questions",
  "outline_node_id": "node_2"
}
```

如果 `tutor_step.questions` 为空，PPT 不补题，只记录 warning。

## 7. slide_type 选择规则

`slide_type` 是模板类型，不是生成顺序。

第一版可以用规则判断：

- `objectives`: 小节标题或内容明显是学习目标。
- `comparison`: `key_points` 或标题中有“对比”、“区别”、“vs”、“鉴别”。
- `process`: 标题或内容中有“机制”、“流程”、“演变”、“步骤”。
- `summary`: 标题或内容是总结、回顾、要点归纳。
- `quiz`: 专门的小测页，第一版可不单独生成。
- `concept`: 默认类型。

封面页 `title` 可选。若生成封面页，它不计入“一节一页”的内容页约束。

## 8. 前端联动方式

前端渲染 PPT 时：

1. 根据 `slides[]` 渲染页面。
2. 当前 slide 有 `outline_node_id`。
3. 用同一个 `outline_node_id` 找到对应 `tutor_step.teaching_text`。
4. 播放或展示讲稿。
5. 如用户选择小测，用同一个 `outline_node_id` 找到 `tutor_step.questions`。
6. renderer 根据 `layout_plan` 决定 blocks 放入 intro / main / support / callout 等区域。

推荐前端状态：

```json
{
  "current_slide_id": "slide_node_2",
  "current_outline_node_id": "node_2",
  "current_narration_source": "tutor_step.teaching_text"
}
```

## 9. 后端实现建议

第一阶段已新增独立 `slides.py`，不要直接大改旧 `build_slides()`。旧 `resource_factory.build_slides()` 是 outline-based legacy placeholder，不是新 PPT 正文路径。

已存在/建议保留的聚合函数：

```text
build_slides_from_handout_and_tutor(
  session_id,
  outline,
  handout_sections_by_node_id,
  tutor_steps_by_node_id
) -> slides_json
```

后续建议补单节生成与缓存：

```text
build_slide_from_handout_section(
  session_id,
  outline_node,
  handout_section,
  tutor_step | None
) -> slide_json

build_and_save_slide_for_node_from_session_cache(
  session_id,
  outline,
  outline_node_id,
  resource_root
) -> resource_result

aggregate_slides_from_node_cache(
  session_id,
  outline,
  resource_root
) -> slides.v1_json
```

推荐位置：

```text
backend/services/learning/slides.py
```

Resource Factory 可以提供资源层入口，但 Orchestrator 接入应后置，避免影响课程推进。

## 10. 验收标准

本地开发完成后至少验证：

- `slides.json.schema_version == "slides.v1"`。
- 每张内容页都有 `outline_node_id` 和 `section_id`。
- 每张内容页满足 `outline_node_id == section_id`。
- 内容页数量等于已有有效 `handout_section` 数量。
- 单节 slide 可在对应 `handout_section` ready 后独立生成。
- 聚合 deck 允许部分节点缺失并记录 warnings。
- 每张内容页 title 来自 handout section 或 outline node。
- `bullets` 来自 handout key point heading，不是 outline title 硬编。
- `narration_ref.source == "tutor_step.teaching_text"`。
- 如果存在小测，`quiz_ref.source == "tutor_step.questions"`。
- 输出 JSON 不包含 `transition_to_next`。
- 空 `clinical_example` 不生成 `clinical_example` block。
- 空 `common_confusions` 不生成 `confusion` block。
- 每张 slide 有 `layout_plan` 或可由规则 planner 稳定生成。
- 预览 renderer 按区域排版，不把 blocks 当网页信息流从上到下堆叠。
- 不修改 Tutor prompt 和 Tutor 生成逻辑。
- 不修改课程推进逻辑。

## 11. 建议本地测试

可以新增一个轻量 smoke：

```text
scripts/smoke_test_learning_slides.py
```

测试内容：

1. 构造 2-3 个固定 handout sections。
2. 构造对应 tutor steps。
3. 调用 slides 生成函数。
4. 断言一节一页、字段齐全、引用正确。
5. 断言可选模块为空时不生成空 block。
6. 断言 `transition_to_next` 不进入输出。
7. 断言 layout plan / 基础区域渲染结构存在。

第一版不需要真实 LLM，也不需要启动前端。

## 12. 开发顺序

推荐顺序：

1. 写固定 fixture 和 smoke test。
2. 新增 `build_slides_from_handout_and_tutor()`。
3. 生成 `slides.v1` JSON。
4. 接入资源保存或 API 返回。
5. 补 per-node slide cache：`slides/{outline_node_id}.json`。
6. 补聚合器：`slides/{node_id}.json -> slides/slides.v1.json`。
7. 补 `layout_plan` 规则 planner。
8. 本地预览器按 `layout_plan` 做基础 PPT 区域排版。
9. 后续再考虑 LLM 展示整理、母版设计和 `pptx` 导出。

## 13. 常见误区

- 不要把 `script_segments` 当成新的讲稿生成任务。
- 不要把一个 handout section 自动拆成多页。
- 不要为了 PPT 美观丢掉 `outline_node_id`。
- 不要让 PPT 生成器重新问 LLM 编医学正文。
- 不要在 PPT 侧重新生成 quiz。
- 不要把 `slide_type` 理解为固定页面顺序。
- 不要等待全课所有节点完成后才生成第一节 PPT。
- 不要让 slide 依赖 tutor_step 先生成；slide 主体内容来自 handout_section。
- 不要把预览器固定模板当作自动生成效果。
- 不要只按 blocks 顺序纵向堆叠，必须经过 layout_plan / 区域排版。

## 14. 当前第一版结论

本地开发最重要的一句话：

```text
一节讲义 -> 一张 PPT 内容页；
一节讲义 -> 一节讲稿；
通过 outline_node_id 绑定；
PPT 只做结构化呈现和基础排版，不改变讲义和讲稿生成逻辑。
```

当前阶段的工程重点：

```text
handout_section -> slide blocks -> layout_plan -> renderer preview
```

最终精细母版、完整模板样式和高级组件设计可以由队友后续完善；但当前链路必须保证基础 PPT 排版成立，不能像网页列表一样乱堆。
