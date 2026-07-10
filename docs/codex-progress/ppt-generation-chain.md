# PPT 生成链路进展记录

工作区：`C:\Users\luzhiyi\OralSage`

项目目标：完成 OralSage PPT 生成链路。

当前分支：`codex/ppt-only-development-agentflow`

## 当前模板边界

- 正式 PPT 模板候选池只看 `tools/ppt_preview/templates/README_zh.md` 中标记为 active 的模板。
- 当前 active 候选是 `tools/ppt_preview/templates/pin-and-paper/`。
- `tools/ppt_preview/index.html`、`tools/ppt_preview/js/`、`tools/ppt_preview/styles/`、`sample-slides.v1.json`、`stress-slides.v1.json` 以及 `scripts/smoke_test_ppt_preview_*` 属于早期规则预览沙盒，只保留作历史调试和回归参考。
- 后续文档、任务和验收不要把早期规则预览沙盒作为正式模板候选，也不要用它证明真实讲义可以适配当前 PPT 模板。
- `soft-editorial` 是 reference-only 试验模板，暂不作为 active 候选，除非用户明确要求重新启用。

## 当前完成概况

### 子任务 1：无 LLM 的 slides.v1 生成器

状态：已审查通过。

完成内容：

- 新增 `backend/services/learning/slides.py`。
- 新增 `build_slides_from_handout_and_tutor()`。
- 从 `handout_section + tutor_step` 生成 `slides.v1`。
- 输出粒度为 `one_slide_per_outline_node`。
- 每张内容页固定绑定 `outline_node_id == section_id`。
- `slide_type` 第一版固定为 `mixed_learning`。
- `blocks` 从 `content.overview/key_points/clinical_example/common_confusions` 派生。
- 不调用 LLM，不生成 pptx，不修改前端。

### 子任务 2：从 session 缓存生成并保存 slides.v1

状态：已审查通过。

完成内容：

- 增加从 session 缓存读取已生成讲义和讲稿的接入层。
- 输入缓存来自 `handout_sections/{outline_node_id}.json` 与 `tutor_steps/{outline_node_id}.json`。
- 正确读取缓存 record 包裹结构：讲义在 `record["handout_section"]`，讲稿在 `record["tutor_step"]`。
- 将生成结果保存到 session 目录下的 `slides/slides.v1.json`。
- 返回 `status/resource_type/resource_path/resource_json/warnings`。
- smoke test 使用临时目录，没有写入真实 `data/learning/sessions`。
- 返工后确认 PPT 输出 JSON 中不再包含 `transition_to_next` 或过渡句文本。

### 子任务 3：接入 Resource Factory 资源入口

状态：已审查通过。

完成内容：

- 在 Resource Factory 层新增从 session 缓存生成 slides 资源的入口。
- 新入口调用已通过审查的 session cache slides 生成器。
- 输出仍为 `slides/slides.v1.json`，不写旧路径 `slides.json`。
- 返回资源层 dict：`status/resource_type/resource_path/resource_json/warnings`。
- 旧的 outline-based `build_slides()` 只标记为 legacy placeholder，未接入新 PPT 正文路径。
- 不修改 Orchestrator，不触发课程推进，不调用 LLM，不调用 Tutor 生成。
- smoke test 增加 Resource Factory entry 覆盖，使用临时目录。

### 子任务 4：可选讲义模块测试

状态：已审查通过。

完成内容：

- 增加 `optional handout modules` smoke test。
- 验证 `clinical_example` 为空时不生成 `clinical_example` block。
- 验证 `common_confusions` 为空数组时不生成 `confusion` block。
- 验证 `overview` 和 `key_points` 仍正常生成展示 block。
- 验证输入中保留的 `transition_to_next` 不进入 PPT 输出。

### 子任务 5：PPT Preview 基础自适应布局

状态：已审查通过。

完成内容：

- 新增 `tools/ppt_preview/` 静态预览器，用于查看 `slides.v1` 的 HTML 版 16:9 课件页。
- 预览器采用 OralSage 当前主页风格：浅紫背景、白色画布、品牌色、统一页眉页脚。
- 修复 JS/CSS composition class 命名不一致导致的重叠问题。
- 增加规则版 `layout_plan` 展示：`composition`、`density`、`regions`。
- 支持 `main_only`、`dense`、`bottom_support`、`side_support`、`balanced` 等基础 composition。
- 返工后修正少量 key_points 和多个 key_points 页面只堆在顶部的问题。
- 第 2 页无 support + 2 个 key_points 已能用较舒展的双卡布局占用主体区域。
- 第 3 页无 support + 5 个 key_points 已能用 dense 网格分布到主体区域。
- 新增真实浏览器 smoke test，生成每页截图并检查 16:9、区域不重叠、区域不溢出、无内部滚动条和 key point 区域覆盖率。

### 子任务 6：PPT Preview 架构拆分与模块化验收

状态：已审查通过。

完成内容：

- 将 `tools/ppt_preview/app.js` 的大文件逻辑拆到 `tools/ppt_preview/js/` 下的职责模块。
- `js/app.js` 只保留 app shell：DOM refs、deck/slideIndex 状态、上一页/下一页、键盘导航、JSON textarea 渲染、bootstrap。
- `js/data-loader.js` 负责默认加载 `stress-slides.v1.json`，不存在时 fallback 到 `sample-slides.v1.json`。
- `js/text-utils.js` 负责 `escapeHtml`、`truncateText` 等纯文本工具。
- `js/block-classifier.js` 负责 `classifyBlocks`，只把 blocks 分成 overview/main/support。
- `js/layout-planner.js` 负责 `buildLayoutPlan`、flow keyword、support display chars 和 layout_archetype/density/regions/visual_role。
- `js/display-planner.js` 负责 `getDisplayText`、`buildDisplayPlan` 和 display budget/warning。
- `js/renderers/block-renderers.js` 负责 overview/key_point/clinical/confusion 单块 HTML 以及共享 render helper。
- `js/renderers/archetype-renderers.js` 负责 8 种 archetype body renderer。
- `js/renderers/standard-learning-renderer.js` 负责顶层 standard_learning slide frame/header/footer/debug 组装。
- 旧 `tools/ppt_preview/app.js` 缩减为兼容 stub，不再承载 planner/display/renderer 大逻辑。
- 将 `tools/ppt_preview/styles.css` 拆为 7 个 CSS 文件，并保留 `styles.css` 为 `@import` 汇总入口。
- 更新 `scripts/smoke_test_ppt_preview_assets.py`，增加新 JS/CSS 模块存在性、script 顺序、CSS import、旧 app stub、模块职责和 JS/CSS archetype 一致性检查。
- `scripts/smoke_test_ppt_preview_render.py` 保持真实浏览器渲染验收，12 页 stress deck 截图可生成。
- 本轮没有有意改变视觉行为；目标是架构拆分与职责迁移。

### 子任务 7：单节 slide 缓存与课程级聚合器

状态：已审查通过。

完成内容：

- 在 `backend/services/learning/slides.py` 中新增 `build_slide_from_handout_section()`，支持从单个 `handout_section` 和可选 `tutor_step` 构建一张内容 slide。
- 新增 `build_and_save_slide_for_node_from_session_cache()`，从指定 `resource_root` 下读取单节点 handout/tutor cache，并保存到 `slides/{outline_node_id}.json`。
- 新增 `aggregate_slides_from_node_cache()`，按 outline 顺序读取已有单节 slide cache，聚合为 `slides.v1` deck dict。
- 重写 `build_and_save_slides_from_session_cache()` 为两步流水线：先生成/保存已有 handout 对应的单节 slide cache，再从单节 cache 聚合并保存 `slides/slides.v1.json`。
- 聚合器不会倒逼缺失节点生成；缺失单节 slide cache 时写入 `missing_slide_cache: {node_id}` warning。
- `tutor_step` 缺失不阻塞 slide 主体生成；`narration_ref` / `quiz_ref` 仍指向对应 `outline_node_id`，并写入 tutor missing warning。
- 保持不写旧路径 `slides.json`。
- 更新 `scripts/smoke_test_learning_slides.py`，新增单节构建、单节点保存、部分聚合、tutor 缺失、两步流水线、可选模块经单节缓存路径的测试。
- 测试使用临时目录，没有写入真实 `data/learning/sessions`。

### 子任务 8：Soft Editorial 中文模板 LLM planner 契约与预算重试

状态：已审查通过。

完成内容：

- 新增 `backend/services/learning/soft_editorial_planner.py`，作为 `slides.v1` 到 Soft Editorial `page_type + slots` 的独立 LLM planner/adapter。
- LLM client 可注入，当前 smoke test 全部使用 fake LLM，不调用真实外部 API。
- 单页流程为两阶段：首次生成完整 page plan；若 slot/card/cell 超预算，则把原始 slide、首次完整 plan、budget failures 和预算表再次发给 LLM，要求返回整页修订 plan。
- Python 负责 prompt 组装、JSON 解析、page_type/slot 校验、HTML/CSS 检查、预算检查、最多一次 compression retry 和 warnings 聚合。
- Python 和 renderer 都不截断文本；retry 后仍超预算时保留完整文本，并写入 `soft_editorial_over_budget_after_retry: {source_slide_id}`。
- `slide-types.json` 为 12 个 Soft Editorial page_type 增加中文可读说明 `purpose_cn`，保留英文 `type` 作为稳定代码 ID。
- `llm-contract.md` 增加两阶段 API flow、文本预算表、Python 职责、retry 后行为和 renderer 不截断边界。
- 返工修复 `matrix.columns` / `chart.legend` 等 primitive array slot 被误判为 dict 的问题。
- 返工补齐 `matrix.rows[*].cells[*]`、`chart.series[*].values[*]`、`chart.legend[*]`、`matrix.columns[*]` 的预算检查和 retry 覆盖。
- 新增 `scripts/smoke_test_soft_editorial_planner.py`，覆盖正常生成、非法 page_type、HTML/CSS 拒绝、超预算 retry、retry 后仍超预算、deck 级规划、compression prompt 上下文、空 slot、method steps、LLM 异常、matrix/chart primitive arrays 和 matrix/chart 超预算 retry。
- 本轮不接真实 LLM API，不生成 PPTX，不接 Orchestrator，不改 renderer，不写真实 data。

### 子任务 9：Pin & Paper 模板与生产形态讲义适配验收

状态：已审查通过。

完成内容：

- 引入 Pin & Paper 模板目录，用于替代前一版过于花哨且不适合多知识点并列的 Soft Editorial 视觉方向。
- 新增 Pin & Paper 的 `slide-types.json`、`llm-contract.md`、本地 `preview.html` 和中文 fixture。
- 新增 `backend/services/learning/pin_and_paper_planner.py`，作为 `slides.v1` 到 Pin & Paper `page_type + slots` 的独立 LLM planner/adapter。
- Planner 使用可注入 LLM client；当前 smoke test 使用 fake LLM，不调用真实外部 API。
- 单页流程保持两阶段：首次生成完整 page plan；若文本超预算，把整页 plan 连同预算失败信息发回 LLM 做完整重写，Python 和 renderer 都不截断文本。
- 增加生产形态 fixture：从纯 OralSage `handout_sections.v1` 出发生成 `production-slides.v1.json`，再生成 `production-pin-and-paper-plan.v1.json`，避免用已经适配模板的 downstream fixture 冒充真实链路。
- 返工后要求每个数组项携带 `source_block_ids`，并允许该追溯字段通过 planner 校验和预算检查。
- 返工后 fake LLM 从 prompt 内的 `SOURCE SLIDE` JSON 读取真实 slide 内容，不再按 node 硬编码模板友好内容。
- `preview.html#production` 可加载生产形态 plan fixture，用于查看真实讲义字段经 adapter 后的展示结果。

本次审查确认：

- `production-handout-sections.v1.json` 不含 `page_type`、`slots`、`cards`、`steps`、`cta` 等模板字段。
- `production-slides.v1.json` 由 `build_slides_from_handout_and_tutor()` 生成。
- `production-pin-and-paper-plan.v1.json` 由 Pin & Paper planner 生成。
- 8 张 production plan 均能追溯到对应 source slide。
- `cards`、`steps`、`columns` 中的 `source_block_ids` 均引用本 slide 存在的 `block_id`。
- `node_1` plan 不再混入 `node_4` 的危险因素内容或引用，例如 `J Clin Periodontol`、`遗传因素约占`、`吸烟`、`糖尿病`。
- 5 个知识点页面路由到 `cta`，未落到最多 4 卡的 `notes`。
- 本轮仍未接真实 LLM API；通过的是 adapter 边界、prompt 契约、fake LLM 来源追溯和本地预览链路。

### 子任务 10：Pin & Paper production 预览加载返工

状态：已审查通过。

完成内容：

- 修复 `preview.html#production` 被 `deck-stage.js` 改写为页码 hash 后回退到手写 fixture 的问题。
- 在 `deck-stage.js` 加载前捕获 fixture 意图到 `window.__PIN_PAPER_MODE__`。
- `getFixturePath()` 和 `getFixtureLabel()` 改为读取捕获后的 mode，而不是实时读取 `location.hash`。
- 新增 `preview.html?fixture=production` 作为更稳的 production 入口；query param 不受页码 hash 改写影响。
- production 模式注入 `MODE: PRODUCTION` badge，便于人工确认当前不是手写 demo。
- 新增 `scripts/smoke_test_pin_and_paper_preview.py`，通过本地 HTTP server 和浏览器测试验证默认 fixture、`#production`、`?fixture=production` 与 hash rewrite 后的运行态。

本次审查确认：

- `python scripts\smoke_test_pin_and_paper_template.py` 通过。
- `python scripts\smoke_test_pin_and_paper_planner.py` 通过。
- `python scripts\smoke_test_pin_and_paper_preview.py` 通过。
- HTTP server 日志显示 `preview.html#production` 和 `preview.html?fixture=production` 均请求 `fixtures/production-pin-and-paper-plan.v1.json`。
- 实际 Chrome 运行态中，production 预览第一页为 `slide s-notice`，不是手写 `s-cover`。
- 正向 marker `牙周炎的定义与诊断标准` 出现在最终可见文本中。
- 反向 marker `掌握核心概念，建立临床思维。` 没有出现在 production 输出中。
- 页面显示 `MODE: PRODUCTION` badge。

### 子任务 11：Pin & Paper generalized 真实链路与 2/3/4/5 KP 验收返工

状态：已审查通过。

完成内容：

- 修复 generalized fixture 之前 handout 与 plan/slides 主题不一致的问题。
- `production-generalized-handout-sections.v1.json` 现在是高血压生产形态 handout，且不含 `page_type`、`slots`、`cards`、`steps` 等模板字段。
- 从该 generalized handout 重新生成 `production-generalized-slides.v1.json` 和 `production-generalized-plan.v1.json`。
- generalized 样例覆盖 2、3、4、5 个 key points，分别用于验证不同数量的知识点路由。
- 路由结果为：2 KP -> `notes_2up`，4 KP -> `notes_4up`，3 KP -> `notes`，5 KP -> `cta`。
- 修正 `preview.html` generalized 模式文案，去掉旧的糖尿病/非牙周炎误导性 label。
- 修正 preview smoke test 的反向 marker，使用 `牙周炎的定义与诊断标准` 作为 production-only 反向 marker，避免被说明文案误伤。
- 修正测试输出中的 Windows GBK 不兼容字符，避免 UnicodeEncodeError 掩盖真实断言结果。

本次审查确认：

- generalized handout、slides、plan 均包含正向 marker `高血压` 和 `靶器官损害`。
- generalized handout、slides、plan 均不包含旧 in-memory 样例 marker `糖尿病`。
- generalized slides 和 plan 均不包含 production-only marker `牙周炎的定义与诊断标准`。
- `preview.html?fixture=generalized` 运行态实际加载 `fixtures/production-generalized-plan.v1.json`。
- 预览运行态显示 `MODE: GENERALIZED`，并带有 `data-pin-paper-mode="generalized"`。
- generalized 预览最终文本包含 `高血压` 和 `靶器官损害`，不包含 `糖尿病`、`牙周炎的定义与诊断标准` 或手写 demo 文案 `掌握核心概念，建立临床思维。`。
- generalized DOM 中存在 `notes-2up` 和 `notes-4up` grid，证明 2/4 知识点固定模板进入实际渲染。
- 三条验收命令均通过：`python scripts\smoke_test_pin_and_paper_template.py`、`python scripts\smoke_test_pin_and_paper_planner.py`、`python scripts\smoke_test_pin_and_paper_preview.py`。

### 子任务 12：Pin & Paper 真实 LLM 最小闭环与 deepseek-chat 专用配置

状态：已审查通过。

完成内容：

- 新增/完善 Pin & Paper 专用真实 LLM client adapter。
- Pin & Paper PPT planner 现在优先读取 `PIN_AND_PAPER_LLM_*`，没有专用变量时读取项目 `.env` 中的 `LEARNING_LLM_API_KEY` / `LEARNING_LLM_BASE_URL`。
- PPT planner 默认模型独立设为 `deepseek-chat`，不改变全局 `LEARNING_LLM_MODEL=deepseek-v4-pro`，也不修改 `.env`。
- 新增真实 LLM smoke script：从 production / generalized handout 出发，生成 slides.v1，再并发调用真实 LLM 生成 real plan。
- 保持并发默认值为 3，输出仍按 source slide 顺序聚合。
- 强化 item-count page_type invariant：
  - exactly 2 KP -> `notes_2up`
  - exactly 4 KP -> `notes_4up`
  - 3 KP 不得使用 `notes_2up` / `notes_4up`
  - 5+ KP 不得使用 `notes` / `notes_2up` / `notes_4up`
- 在 planner 中补充 route correction retry 能力：当真实 LLM 违反 item-count invariant 时，按通用规则要求整页修订；本次真实运行未触发该 retry。
- 生成真实 LLM plan：
  - `tools/ppt_preview/templates/pin-and-paper/fixtures/real-llm-production-plan.v1.json`
  - `tools/ppt_preview/templates/pin-and-paper/fixtures/real-llm-generalized-plan.v1.json`
- 生成真实 LLM slides 中间产物：
  - `real-llm-production-slides.v1.json`
  - `real-llm-generalized-slides.v1.json`
- `preview.html?fixture=real-production` 和 `preview.html?fixture=real-generalized` 均可加载真实 LLM plan。
- 修复 real-production preview 反向 marker：不用宽泛 `高血压`，因为 production handout 源内容中会合法提到高血压/糖尿病作为慢性病对比；改用更具体的 generalized-only marker。

本次审查确认：

- 重新执行四条验收命令均通过：
  - `python scripts\smoke_test_pin_and_paper_template.py`
  - `python scripts\smoke_test_pin_and_paper_planner.py`
  - `python scripts\smoke_test_pin_and_paper_real_llm.py`
  - `python scripts\smoke_test_pin_and_paper_preview.py`
- 实际真实 LLM provider 配置为 DeepSeek OpenAI-compatible API，base URL 为 `https://api.deepseek.com`，模型为 `deepseek-chat`，并发为 3；未泄露 API key。
- real-production page_type 序列为：
  `notes`, `notes_4up`, `process`, `notes`, `notes`, `cta`, `notes`, `notes`。
- real-generalized page_type 序列为：
  `notes_2up`, `notes_4up`, `notes`, `cta`。
- 所有 3KP 页面均未使用 `notes_2up` 或 `notes_4up`。
- 5KP 页面分别使用 `process` 或 `cta`，未使用 notes 系列。
- source_block_ids 追溯检查通过，数组项引用均指向对应 source slide 内真实存在的 block_id。
- real-production runtime source 为 `fixtures/real-llm-production-plan.v1.json`，badge 为 `MODE: REAL-LLM PRODUCTION`。
- real-generalized runtime source 为 `fixtures/real-llm-generalized-plan.v1.json`，badge 为 `MODE: REAL-LLM GENERALIZED`。
- real-production 正向 marker `牙周炎` 出现在 DOM，demo / generalized-only marker 未出现。
- real-generalized 正向 marker `高血压` / `靶器官损害` 出现在 DOM，production-only / demo / old synthetic marker 未出现。
- real preview 测试确认无内部滚动，标题长度满足约束，real-generalized DOM 中存在 `notes-2up` 和 `notes-4up`。

## 重要决策

- PPT 第一版不是新的医学内容生成器，只做结构化呈现。
- 内容真源是 LLM 已生成的 `handout_section.content`。
- `handout_section` 指单节讲义，是每个 outline node 的内容母本。
- 学习链路是按单节推进和缓存，不应要求全课所有节点完成后才生成 PPT。
- 正确依赖关系是：
  `outline node -> handout_section/{node_id}.json -> tutor_step/{node_id}.json`
  与
  `outline node -> handout_section/{node_id}.json -> slide/{node_id}.json`。
- 讲稿和 PPT 是从同一节讲义并行派生；PPT 不应依赖讲稿先生成。
- `tutor_step` 可以被 PPT 通过 `narration_ref` / `quiz_ref` 引用，但不是 PPT 主体内容来源。
- 已补 per-node slide cache：`slides/{node_id}.json`；课程级 `slides/slides.v1.json` 是聚合视图，由已有单节 slide 按 outline 顺序拼成。
- 第一节先生成、后续节点后台慢慢补的设定下，node_1 的 slide 应在 node_1 的 handout_section ready 后即可生成，不等待 node_2/node_3。
- 讲稿只通过 `tutor_step.teaching_text` 引用，不在 PPT 侧重写。
- 小测只通过 `tutor_step.questions` 引用，不在 PPT 侧补题。
- 一小节一页，因此第一版采用混合学习页 `mixed_learning`，允许知识点、病例和易混点共存。
- `clinical_example` 和 `common_confusions` 是可选增强模块，不是每页固定区域。
- PPT blocks 只为非空内容生成，不硬凑空模块。
- 过渡句 `transition_to_next` 属于上游讲义字段，可以保留在 handout cache 中，但不进入 `slides.v1` 输出。
- `slides.v1.content` 是 PPT 展示层内容投影，不再是 `handout_section.content` 的完整原样镜像。
- 当前阶段主要开发 PPT 生成链路和基础排版逻辑，不完整实现所有精细母版。
- 最终母版、不同类型模板样式和高级组件视觉可由队友后续完善。
- 但当前 renderer 仍必须像 PPT：固定 16:9 画布、标题区、主内容区、辅助区、页脚、长文本保护和空模块不占位。
- 当前 preview 布局达到的是基础可接受效果，不代表最终视觉模板完成。
- 基础 renderer 可以用规则版 planner 先跑通，但必须避免固定某几页样例；判断依据应是模块数量、字数和类型。
- 预览器固定模板的视觉效果不等于自动生成效果；自动生成效果要通过 `layout_plan` 和真实 handout 输入评估。
- 后续每张 slide 应包含或可稳定生成 `layout_plan`：`blocks -> intro/main/support/callout` 等区域映射。
- 规则 planner 和未来 LLM planner 应输出同一种 `layout_plan`；renderer 不关心 planner 来源。
- LLM 后续只做展示整理、模板选择、模块排序和短句压缩，不直接写 HTML/CSS，不新增医学内容。
- Soft Editorial planner 已建立 fake LLM 可验证的 API adapter 边界，但尚未接真实 LLM provider。
- 超长文本处理必须是整页二次 LLM 压缩，不允许 Python 或 renderer 直接截断。
- 卡片页不能为套模板硬凑内容；2/4/5 个并列知识点后续应做模板数量变体，其中 5 卡推荐使用左侧大卡 + 右侧 2x2 小卡的 mosaic 构图，不做两行三列缺一格。
- `insights` / mosaic 不能成为默认兜底页型；后续需要页型路由与多样性约束，避免整套 PPT 长得一样。
- 子任务审查通过后由 Codex 维护本进展文档，不让 Claude Code 自行写项目过程文档。

## 审查逻辑

本次审查实际执行：

- `git status --short --branch`
- `git log --oneline --decorate --max-count=5`
- 读取 `backend/services/learning/slides.py`
- 读取 `scripts/smoke_test_learning_slides.py`
- 确认 `docs/project-notes.md` 已删除
- `python scripts\smoke_test_learning_slides.py`
- 检查 Claude Code 报告中的完整 `slides.v1.json`
- 检查输出 JSON 不含 `transition_to_next` 和具体过渡句文本
- 检查 Resource Factory 新入口未改 Orchestrator、Tutor、前端和 prompts
- 检查可选模块测试中的完整 slide JSON
- 读取 `tools/ppt_preview/app.js`
- 读取 `tools/ppt_preview/styles.css`
- 读取 `scripts/smoke_test_ppt_preview_assets.py`
- 读取 `scripts/smoke_test_ppt_preview_render.py`
- `python scripts\smoke_test_ppt_preview_assets.py`
- `python scripts\smoke_test_ppt_preview_render.py`
- 查看 `tools/ppt_preview/artifacts/slide_1.png`
- 查看 `tools/ppt_preview/artifacts/slide_2.png`
- 查看 `tools/ppt_preview/artifacts/slide_3.png`
- 按 `delegating-and-reviewing-tasks` 补充审查 PPT Preview 架构拆分完成报告。
- `git status --short --branch`
- `git log --oneline --decorate --max-count=5`
- 检查 `tools/ppt_preview/js/` 和 `tools/ppt_preview/styles/` 模块文件。
- 检查旧 `tools/ppt_preview/app.js` 不再包含 `buildLayoutPlan`、`buildDisplayPlan`、`renderBody*`、`renderSlideStandardLearning` 大逻辑。
- 重新执行 `python scripts\smoke_test_ppt_preview_assets.py`。
- 重新执行 `python scripts\smoke_test_ppt_preview_render.py`，生成 `tools/ppt_preview/artifacts/stress_slide_01.png` 到 `stress_slide_12.png`。
- 按 `delegating-and-reviewing-tasks` 审查单节 slide cache + 聚合器完成报告。
- `git status --short --branch`
- `git log --oneline --decorate --max-count=5`
- 检查 `backend/services/learning/slides.py` 中的 `build_slide_from_handout_section`、`build_and_save_slide_for_node_from_session_cache`、`aggregate_slides_from_node_cache`。
- 检查 `scripts/smoke_test_learning_slides.py` 中 Test 5-10 覆盖单节构建、单节保存、部分聚合、tutor 缺失、两步流水线和可选模块。
- 重新执行 `python scripts\smoke_test_learning_slides.py`。
- 按 `delegating-and-reviewing-tasks` 审查 Soft Editorial planner 完成报告。
- `git status --short --branch`
- `git log --oneline --decorate --max-count=5`
- 检查 `backend/services/learning/soft_editorial_planner.py` 中 LLM client 注入、两阶段 prompt、slot 校验、HTML/CSS 检查、预算检查、最多一次 compression retry 和 deck 聚合。
- 检查 `scripts/smoke_test_soft_editorial_planner.py` 中 16 个 fake LLM smoke case。
- 重新执行 `python scripts\smoke_test_soft_editorial_planner.py`。
- 用一次性 matrix fixture 复现上一轮问题，确认 `matrix.columns` 字符串数组不再被误判，`matrix.rows[*].cells[*]` 超预算会触发二次压缩。
- 用一次性 chart fixture 确认 `chart.series[*].values[*]` 超预算会触发二次压缩。

审查结果：

- 无 forbidden commit，HEAD 仍为 `de0d027`。
- `docs/project-notes.md` 已删除。
- 未修改 `frontend/*`。
- 未修改 Tutor、Resource Factory、Orchestrator、prompts。
- smoke test 通过，最后一行为 `learning slides smoke test passed`。
- smoke test 完整输出已贴在聊天窗口。
- session cache 生成链路通过，保存路径为临时目录下的 `slides/slides.v1.json`。
- 过渡句删除返工通过：输入 fixture 仍有 `transition_to_next`，但输出 slides JSON 中没有该字段或文本。
- Resource Factory entry 通过：新入口输出 `slides/slides.v1.json`，未写旧 `slides.json`。
- 可选模块测试通过：空病例、空易混点不会生成空展示块。
- PPT preview 基础布局通过：三页样例均为固定 16:9，未重叠，未出现 slide 内部滚动条。
- 第 2 页和第 3 页已不再是顶部列表加下方大片空白。
- 当前预览服务需要手动启动：`python -m http.server 8765 -d tools/ppt_preview`。
- PPT preview 架构拆分通过：JS 已按 data-loader / text-utils / block-classifier / layout-planner / display-planner / renderers / app shell 拆分。
- CSS 拆分通过：`styles.css` 仅保留 7 个 `@import`，具体规则移动到 `styles/base.css`、`shell.css`、`slide-frame.css`、`typography.css`、`components.css`、`archetypes.css`、`responsive.css`。
- `index.html` 已按依赖顺序加载 `js/*.js`，不再加载旧 `app.js`。
- 旧 `tools/ppt_preview/app.js` 已是兼容 stub，不再包含 planner/display/archetype renderer/standard renderer 完整实现。
- 静态验收通过，最后一行为 `ppt preview assets smoke test passed`。
- 渲染验收通过，最后一行为 `ppt preview render smoke test passed`。
- 12 页 stress deck 截图路径为 `tools/ppt_preview/artifacts/stress_slide_01.png` 到 `stress_slide_12.png`。
- 本次 review 发现工作树仍有 backend 相关未提交改动；这些来自前序 PPT 生成链路任务，不属于本次 preview 拆分的新增修改。
- 单节 slide cache + 聚合器通过：`python scripts\smoke_test_learning_slides.py` 最后一行为 `learning slides smoke test passed`。
- 单节 slide 保存路径通过：`slides/{outline_node_id}.json`。
- 课程级聚合保存路径通过：`slides/slides.v1.json`。
- 部分节点缺失不阻塞通过：聚合器输出已有 slide，并在 deck warnings 中写入 `missing_slide_cache: node_2` 等 warning。
- `tutor_step` 缺失不阻塞通过：slide 主体仍生成，`narration_ref` / `quiz_ref` 保留 outline_node_id，warnings 记录 tutor 缺失。
- 未写旧路径 `slides.json`。
- Soft Editorial planner smoke test 通过，最后一行为 `soft editorial planner smoke test passed`。
- Soft Editorial planner 仍使用 fake LLM，不调用真实外部 API。
- Matrix/chart 返工通过：primitive array slot 不再被误判为 dict；matrix cell、chart values、legend 超预算均会触发整页 compression retry。
- 本轮未修改 `frontend/*`、真实 `data/*`、Orchestrator、Tutor、QA、Assessment、prompts 或 preview renderer 核心文件。
- 按 `delegating-and-reviewing-tasks` 审查 Pin & Paper 生产形态讲义适配返工完成报告。
- `git status --short --branch`
- `git log --oneline --decorate --max-count=5`
- 检查 `backend/services/learning/pin_and_paper_planner.py` 中 prompt 规则、`source_block_ids` 追溯字段、slot 校验、预算检查和 compression retry。
- 检查 `scripts/smoke_test_pin_and_paper_planner.py` 中 fake LLM 是否从 `SOURCE SLIDE` JSON 读取内容，并检查 Test 12-14 的追溯与跨节污染验证。
- 重新执行 `python scripts\smoke_test_pin_and_paper_template.py`，最后一行为 `pin and paper template smoke test passed`。
- 重新执行 `python scripts\smoke_test_pin_and_paper_planner.py`，最后一行为 `pin and paper planner smoke test passed`。
- 额外执行独立 JSON 检查，确认 production plan 的 `source_block_ids` 全部引用对应 source slide 内真实存在的 `block_id`。
- 额外检查 `node_1` plan 不含 `遗传因素约占`、`糖尿病`、`吸烟`、`J Clin Periodontol`、`危险因素`。
- 按更新后的 User-Visible Artifact Verification Gate 审查 Pin & Paper production 预览加载返工。
- 检查 `tools/ppt_preview/templates/pin-and-paper/preview.html` 中 `window.__PIN_PAPER_MODE__` 是否在 `deck-stage.js` 之前捕获。
- 检查 `scripts/smoke_test_pin_and_paper_preview.py` 是否包含 production 正向 marker 和 hand-written fallback 反向 marker。
- 重新执行 `python scripts\smoke_test_pin_and_paper_preview.py`，最后一行为 `pin and paper preview smoke test passed`。
- 用实际 Chrome 打开 `http://localhost:8766/preview.html?fixture=production`，确认 `MODE: PRODUCTION` badge、`slide s-notice`、`牙周炎的定义与诊断标准` 出现，且 `掌握核心概念，建立临床思维。` 不出现。

## 风险点

- `slides.py` 当前已接入 Resource Factory 资源入口，但尚未接入 Orchestrator 或 API。
- 单节 slide cache 已补齐，但尚未接入 Orchestrator 或 API 的按节点触发流程。
- 当前预览器已能显示 OralSage 风格页面，但基础区域排版和 `layout_plan` 链路仍需补齐，避免 blocks 像网页列表一样堆叠。
- 当前 preview 布局主要靠规则和 CSS 拉伸实现，只能算基础可接受，不是最终母版质量。
- 当前 render test 能检查覆盖率和滚动条，但还不能充分判断长文本被裁切后的可读性。
- 真实讲义字数更不稳定，后续需要增加长讲义、短讲义、仅概述、仅 1 个 key point、多 support 等压力样例。
- 当前 `blocks[*].semantic_role` 固定为 `concept`，尚未做展示整理或语义角色识别。
- 当前 `content` 仍包含 key point 的完整 `explanation`，前端渲染时需要避免把长正文直接铺满页面。
- 后续如果接 LLM，只能做课件展示整理，不能生成新的医学正文。
- Soft Editorial planner 目前是独立 adapter，尚未接真实 LLM provider、API 路由或课程推进流程。
- Pin & Paper planner 已完成真实 LLM 最小闭环，但尚未接 API 路由、Orchestrator 或课程推进流程。
- 当前真实 LLM 验收使用本地 production/generalized fixtures，尚未用线上 session cache 或真实用户课程样本批量验证。
- 当前 deck 级 planner 仍是顺序遍历；教育产品更看重速度，后续需要在不降低校验质量的前提下支持按 slide 并发规划、失败隔离和按 outline 顺序聚合。
- Pin & Paper 当前有 HTML 预览和 plan JSON 产出，但尚未生成 PPTX。
- 模板路由仍由 fake LLM 简化决策，后续真实 LLM 接入时要防止某个宽松页型变成默认兜底。
- `preview.html#production` 当前已修复首轮加载，但地址栏会被 deck-stage 改成 `#1`；需要可持久分享或刷新稳定时，优先使用 `preview.html?fixture=production`。
- Release-candidate boundary audit 已通过：旧 root preview、Soft Editorial、截图产物、旧 preview smoke 脚本均已从默认候选中排除；`tools/ppt_preview/templates/candidates.v1.json` 中 active 仅保留 `pin-and-paper`。
- Pin & Paper template smoke Test 01 已返工：模板目录检查现在只统计目录，不再把 `README_zh.md` 和 `candidates.v1.json` 元数据文件误判为模板目录。
- 当前五条候选验收均通过：`smoke_test_learning_slides.py`、`smoke_test_pin_and_paper_template.py`、`smoke_test_pin_and_paper_planner.py`、`smoke_test_pin_and_paper_real_llm.py`、`smoke_test_pin_and_paper_preview.py`。
- Runtime preview source marker 已验证：real-production 实际加载 `fixtures/real-llm-production-plan.v1.json`，real-generalized 实际加载 `fixtures/real-llm-generalized-plan.v1.json`，且 fallback/demo/另一主题反向 marker 均未出现。

## 下一步建议

下一轮建议先做真实 LLM 预览的人工目视验收，再决定是否进入 PPTX 导出：

- 打开 `http://127.0.0.1:8766/preview.html?fixture=real-production` 和 `http://127.0.0.1:8766/preview.html?fixture=real-generalized`，检查真实 LLM 输出的信息量、标题、版式多样性和 2/4 卡模板视觉效果。
- 若目视质量可接受，下一步做 PPTX 导出或 HTML-to-PPTX 转换探索。
- 若目视质量仍有问题，优先调整 Pin & Paper prompt/slot contract/retry 校验，而不是改 CSS 或手写 fixture。
- 接 API/Orchestrator 前，仍建议先用真实 session cache 做一次 `handout_section -> slides.v1 -> real LLM plan -> preview` 的端到端验收。

## 2026-07-10 集成清理

- 工作区：`C:\Users\luzhiyi\OralSage`；目标：将正式 Pin & Paper 分支合入整合主路径，并移除误加的 courseware/PPTX 导出链路。
- 正式 PPT 分支 `de4c804` 已原样合入；`tools/ppt_preview/templates/pin-and-paper/**` 与远端正式分支逐字一致。
- Orchestrator 现在通过既有 `generate_slides_resource_from_session_cache()` 从 session 的 handout/Tutor cache 生成 `slides/slides.v1.json`；`/api/learning/session/{session_id}/slides` 保留为正式读取入口。
- 已删除临时 `courseware.py`、`tools/ppt_export/**`、PPTX 下载/状态 API、相关导出测试与所有运行时引用；保留音频及 `lecture_script`，不改其主链逻辑。
- 验证：`python -m unittest tests.test_learning_generation_pipeline`（9 项通过）、`python scripts/smoke_test_learning_slides.py`、`python scripts/smoke_test_pin_and_paper_planner.py`、`python scripts/smoke_test_pin_and_paper_template.py` 均通过。独立 preview smoke 在当前环境的浏览器初始化阶段超时；已用实际浏览器验证 real-production 运行时 source 为 `fixtures/real-llm-production-plan.v1.json`，真实标记出现，默认 fixture 标记未出现。
- 风险：真实用户输入的全链路仍待单独验收；Pin & Paper 继续开发时不得重新引入 courseware/PPTX 并行链路。
