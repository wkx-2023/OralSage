# PPT 模板修改准则

本文档是正式 PPT 模板候选池的入口。正式候选清单见 `tools/ppt_preview/templates/candidates.v1.json`，只包括 `tools/ppt_preview/templates/` 下被明确标记为 active 的模板。

当前 active 候选：

- `pin-and-paper`

当前 reference-only / 非 active 候选：

- `soft-editorial`：早期试验模板，因视觉和多知识点适配问题暂不进入正式候选，除非用户明确要求重新启用。

明确排除：

- `tools/ppt_preview/index.html`
- `tools/ppt_preview/app.js`
- `tools/ppt_preview/js/`
- `tools/ppt_preview/styles/`
- `tools/ppt_preview/sample-slides.v1.json`
- `tools/ppt_preview/stress-slides.v1.json`
- `scripts/generate_ppt_preview_stress_fixtures.py`
- `scripts/smoke_test_ppt_preview_assets.py`
- `scripts/smoke_test_ppt_preview_render.py`
- `tools/ppt_preview/artifacts/`
- `backend/services/learning/soft_editorial_planner.py`
- `scripts/smoke_test_soft_editorial_planner.py`
- `scripts/smoke_test_soft_editorial_preview.py`

这些是早期规则预览沙盒、截图产物和 Soft Editorial reference-only 试验路线，不属于正式模板候选池，不用于证明真实讲义适配正式模板。根目录边界说明见 `tools/ppt_preview/README_zh.md`。

本文档给后续修改 PPT 模板的人看。先判断你要做的是哪一种：

- **改变视觉风格**：换颜色、字体、纸张质感、卡片样式、整体气质，但不改变 `page_type + slots` 的数据契约。
- **增加模板能力**：新增或调整 `page_type`、slot schema、LLM 路由规则、renderer 结构，让 PPT 能表达新的内容类型。

这两类任务不要混在一起做。视觉改版应尽量不动 planner；新增模板能力必须同步改契约、校验和测试。

## 共同边界

无论哪一种修改，都必须遵守：

- 内容真源是 `handout_section -> slides.v1`，模板层不得新增医学事实。
- LLM 只负责页型选择、slot 内容整理和必要缩句，不输出 HTML/CSS。
- Python/renderer 负责 schema、slot、预算、source traceability 和 runtime source 校验，不替 LLM 写医学正文。
- renderer 不得通过截断、`overflow:hidden` 或缩到不可读来掩盖超预算；超预算必须让 LLM 整页重写。
- 所有用户可见预览必须验证 runtime 实际加载的 source artifact，并做正向/反向 marker 检查。
- 不要为了当前某页、某个 node、某张截图写特判。

## 情况 A：改变视觉风格

适用场景：

- 觉得 Pin & Paper 的黄色纸张、别针、手写风格不适合，想换成更医疗、更学院、更现代或更产品化的视觉。
- 只换样式，不新增语义页型。
- 仍沿用现有 `notes_2up`、`notes`、`notes_4up`、`cta`、`process`、`notice`、`quote` 等页型和 slots。

修改原则：

- 优先复制一个新模板目录，例如 `tools/ppt_preview/templates/<new-template>/`，不要直接破坏已通过验收的 `pin-and-paper`。
- 如果只是局部调色或字体，也可以在原模板内改，但要明确这是 visual-only change。
- 不修改 `handout_section`、`slides.v1`、Resource Factory、Tutor、QA、Assessment 或 Orchestrator。
- 不新增 page_type，不改 slot shape，不改 source_block_ids 规则。
- 视觉层只改变：
  - 色彩、字体、间距、卡片边框、背景质感。
  - 同一 slot 在画布上的位置和尺寸。
  - 页眉、页脚、装饰元素。
- 不改变：
  - 每种 page_type 需要哪些 slots。
  - 每种数组 slot 的 min/max items。
  - 2/3/4/5 key_points 的路由规则。
  - LLM 输出 JSON schema。

必须同步检查：

- `preview.html` 或 renderer 能加载同一个 plan JSON 并正常渲染。
- 原有 fake LLM plan 和 real LLM plan 都能被新视觉消费。
- 无内部滚动、无内容重叠、标题可读。
- 2 卡、4 卡、5+ 多项页实际可见，不只看 schema。

验收建议：

```powershell
python scripts\smoke_test_pin_and_paper_template.py
python scripts\smoke_test_pin_and_paper_planner.py
python scripts\smoke_test_pin_and_paper_real_llm.py
python scripts\smoke_test_pin_and_paper_preview.py
```

如果新建模板目录，应新增对应 smoke test，而不是只复用 Pin & Paper 名字。

完成报告必须说明：

- 这是 visual-only change。
- 是否改了任何 page_type / slot schema；如果改了，就不是纯视觉改版。
- 哪些真实 plan 用来做了视觉验收。
- 浏览器可打开 URL。

## 情况 B：增加模板能力

适用场景：

- 要新增病例页、对比页、机制流程页、治疗步骤页、时间线页、图表页、小测页、总结页等。
- 要让 LLM 根据内容语义选择不同页面类型，而不是只按知识点数量。
- 要扩展 `page_type + slots` 契约。

修改原则：

- 一个新能力对应一个清晰的 `page_type`，不要用一个宽泛页型兜所有内容。
- 先定义内容语义，再定义视觉：
  - 这个页型解决什么教学表达问题？
  - 它从 `slides.v1.blocks` 的哪些信息来？
  - 哪些输入适合它，哪些输入不适合它？
- 新 page_type 必须同步更新：
  - `slide-types.json`
  - `llm-contract.md`
  - planner prompt / validation
  - renderer / preview
  - fake LLM smoke test
  - real LLM smoke test
  - preview runtime source test
- 不要只加 CSS 或只加一个 demo fixture。

建议优先新增的语义页型：

- `case_study`：病例、患者情境、临床判断。
- `comparison_matrix`：鉴别诊断、分类、分期分级、相似概念比较。
- `mechanism_flow`：病因、病理机制、因果链。
- `treatment_steps`：治疗流程、操作顺序、临床路径。

每个新 page_type 必须写清：

- `purpose_cn`：这页用来表达什么。
- `best_for`：适合哪些内容信号。
- `avoid_when`：不适合哪些内容，防止成为默认兜底。
- `slots`：字段、数组 min/max、每项必填字段。
- `budget`：标题、正文、卡片、步骤、表格单元的字符预算。
- `source_traceability`：哪些 slot item 必须带 `source_block_ids`。

LLM 路由规则必须基于通用信号：

- item count：2/3/4/5+ 个并列知识点。
- block type：`overview`、`key_point`、`clinical_example`、`confusion` 等。
- 内容语义：病例、对比、机制、流程、总结、定义、诊断标准。
- 文本密度：单项正文长短、是否需要 compression retry。

禁止：

- `if node_id == "node_3"`。
- `if title contains "牙周炎"` 之类疾病名特判。
- 按当前截图页码修 CSS。
- 写一个 downstream-shaped fixture 来证明 upstream handout 可适配。

新增模板能力的最低验收：

- 至少两个不同主题样例：
  - production 牙周炎。
  - generalized 高血压或其他非牙周炎主题。
- 至少一个真实 LLM plan。
- 至少一个 fake LLM deterministic test。
- preview runtime source 正反向 marker。
- source_block_ids 全部引用本页 source slide block。
- 新 page_type 的正例和反例测试：
  - 正例：该内容确实选中此页型。
  - 反例：不适合的内容不会被它吞掉。

完成报告必须说明：

- 新增了哪些 page_type。
- 每个 page_type 的适用场景和不适用场景。
- LLM 路由规则是什么。
- 是否发生 route correction retry 或 compression retry。
- 真实预览 URL。
- 是否有任何样例特判。

## 当前 Pin & Paper 的已知状态

当前 Pin & Paper 已通过的核心能力：

- `notes_2up`：exactly 2 key_points。
- `notes`：普通 3 key_points 卡片。
- `notes_4up`：exactly 4 key_points。
- `cta` / `process`：5+ key_points 或流程型内容。
- 真实 LLM 默认使用 `deepseek-chat`，不改变全局学习链路模型。
- production 和 generalized 两套真实 LLM plan 已能在 preview 中加载。

当前不足：

- 页型选择仍主要依赖 key_points 数量。
- 真正按内容语义区分病例、机制、对比、治疗流程的能力还不完整。
- 还没有生成 PPTX。

## 推荐下一步

如果只是“不喜欢现在的视觉”，走 **情况 A：改变视觉风格**。

如果想让 PPT 更像真正教学课件，而不是知识点卡片集合，走 **情况 B：增加模板能力**，优先补：

1. `case_study`
2. `comparison_matrix`
3. `mechanism_flow`
4. `treatment_steps`

每次只新增 1-2 个页型，做完真实 LLM 和浏览器预览验收后再继续。
