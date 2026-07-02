# 课程知识图谱工作流设计

这套流程的目标不是做关键词图谱，而是按“逐教材、逐章、逐页块阅读”的方式，把教材整理成可学习、可追溯、可点击、可继续扩展的知识卡片和知识图谱。

## 当前模型

当前代码通过 `backend/services/llm_client.py` 调用 OpenAI-compatible Chat Completions 接口。环境变量优先级在 `configs/course_graph.yaml` 中定义：

```text
COURSE_GRAPH_LLM_MODEL -> QWEN_MODEL -> DASHSCOPE_MODEL -> OPENAI_MODEL
```

在当前环境中，实际默认走 DashScope 兼容接口，模型是 `qwen-plus`。如果设置 `COURSE_GRAPH_LLM_*`，课程图谱构建会优先使用专用配置，不影响项目里其他 LLM 功能。

## 核心原则

- 只基于教材 OCR 构建，暂时不混入旧 RAG、agent 或其他知识库资料。
- 内容抽取必须由模型阅读教材完成，禁止降级为关键词、正则或死规则匹配。
- 允许工程级恢复：JSON 修复、失败重试、按页拆分、断点续跑、schema 校验。这些只提高稳定性，不替代模型理解。
- 每张知识卡片必须保留教材页码证据。
- 每完成一章，都要回看此前已经形成的卡片和关系，补充跨章串联、重复合并建议和待人工复核项。

## Agent/Workflow 分工

对应的机器可读规格在 `configs/course_graph_agents.yaml`。

1. `TextbookSegmenter`
   读取教材 OCR，并依据配置中的页码范围生成章节文本和元数据。

2. `ChunkReader`
   对长章节进行页感知分块，让模型抽取草稿知识卡片。每个卡片包含类型、标题、摘要、核心点、先修点、易混点和页码证据。

3. `ChapterSynthesizer`
   在同一章内部合并草稿卡片，形成章节级知识卡片、章节大纲和章内关系。代码会从草稿卡片保留证据，避免模型在合并阶段丢证据或凭空扩写。

4. `CrossChapterReviewer`
   把新章节卡片与此前所有章节卡片分批比较，生成跨章关系、合并建议和不确定项。合并建议不会自动删除节点，而是进入 review 队列。

5. `GraphAssembler`
   规范化卡片和关系 ID，写出章节图谱，并在每章成功后重建总图谱。

6. `QualityAuditor`
   下一步应补成独立审计阶段，检查证据覆盖、孤立卡片、近重复标题、关系方向、页码范围、OCR 不确定性和学习目标覆盖。

## 当前输出

章节输出位于：

```text
data/course_graph/chapter_runs/operative_endodontics_zhouxuedong_5e/chapter_XX/
```

总图输出位于：

```text
data/course_graph/master/
```

主要文件包括：

- `cards.jsonl`
- `relations.jsonl`
- `chapter_relations.jsonl`
- `cross_chapter_relations.jsonl`
- `review_queue.jsonl`
- `summary.json`

## 断点续跑和失败恢复

脚本以章节为断点单位。如果某章已有 `llm_result.json` 且没有传 `--force`，会跳过该章。失败章节会从该章重新跑。

模型返回非合法 JSON 时，流程会先让模型只修复 JSON 语法；如果仍失败，会把当前页块拆成更小的页块重新阅读。模型请求失败时也会拆分页块重试。这不是规则降级，因为医学内容仍由模型基于教材文本抽取。

## Codex 是否适合作为模型调用

当前 Codex 是交互式编码智能体，适合帮助设计、修改、审查和运行这套工作流；它不是项目后端中可以稳定直接调用的普通 LLM endpoint。生产流程应调用明确的模型 API，例如当前 DashScope 兼容接口，或切到 OpenAI API 中的强推理/长上下文模型。

上下文爆炸不应该主要靠“换成 Codex”解决，而应该靠层级化工作流解决：

- 页块级抽取只看当前页块。
- 章级合成只看本章草稿索引和证据。
- 跨章 review 使用已有卡片摘要分批比较。
- 总图只保存结构化卡片、关系、证据和 review 队列。

这样上下文不会无限增长，失败也可以从章节或页块恢复。

## 推荐升级

- 保留 `qwen-plus` 作为可用基线，但把章节合成、跨章 review、质量审计切到更强的长上下文/推理模型。
- 增加独立 `QualityAuditor` 脚本，先做结构校验，再做模型二审。
- 增加 UI 层的章节图谱入口：先按教材目录进入章节，再展开卡片、证据页码和跨章连接。
- 把 merge suggestions 做成人工确认队列，确认后再形成 canonical merge。

