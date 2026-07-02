# OralSage 口腔医学课程知识图谱

本图谱是独立于原有 RAG/agent 的课程级知识工程，用于把现有知识库组织成可点击、可拆解、可溯源、可继续增强的学习智能体知识底座。

## 当前构建结果

构建命令：

```powershell
python scripts\knowledge_graph\build_oral_course_graph.py
```

输出目录：

```text
data/processed/knowledge_graph/
```

主要文件：

- `nodes.jsonl`：完整节点表
- `edges.jsonl`：完整关系表
- `nodes.csv` / `edges.csv`：便于导入 Neo4j、Gephi 或其他图工具
- `overview.json`：前端课程总图
- `graph_summary.json`：构建摘要和质量统计
- `issues.jsonl`：构建问题记录

当前规模：

- 知识片段：30,615
- 节点：71,372
- 关系：589,610
- 来源文档：46
- 章节/来源小节：20,878
- 课程主题：19,761
- 构建问题：0

## 图谱层级

1. `Course`
   口腔医学课程总节点。

2. `CourseModule`
   课程模块，包括牙体牙髓、牙周、黏膜、颌面外科、修复、正畸、儿童口腔、种植、影像、预防等。

3. `CurriculumTopic`
   从教材、指南和参考资料的章节/小节标题中规范化抽取的课程主题。

4. 医学概念节点
   包括 `Disease`、`DiseaseGroup`、`Treatment`、`Examination`、`Finding`、`SymptomOrSign`、`Complication`、`Prevention`。

5. `KnowledgeFragment`
   每个知识库 chunk 都是一个可溯源证据片段。

6. `SourceDocument` / `SourceSection`
   来源文档与章节结构。

## 关键关系

- `has_module`：课程包含模块
- `teaches_topic`：模块教授课程主题
- `teaches_concept`：模块教授医学概念
- `supported_by`：主题/概念由知识片段支持
- `mentions_topic` / `mentions_concept`：知识片段提及主题/概念
- `contains_concept`：课程主题包含医学概念
- `covers_dimension`：片段或主题覆盖学习维度
- `treated_by`、`diagnosed_by`、`has_clinical_feature`、`has_risk`：规则抽取的医学关系
- `co_occurs_with`：概念共现关系
- `prerequisite_of`：课程先修关系种子

## 前端入口

启动服务：

```powershell
python -m uvicorn backend.main:app --reload --port 8086
```

访问：

```text
http://127.0.0.1:8086/pages/knowledge-graph.html
```

页面默认展示课程总图，点击课程模块、课程主题或医学概念后会展开节点邻域，并在右侧显示节点属性、相关节点和证据摘录。

## 后端接口

- `GET /api/knowledge-graph/summary`
- `GET /api/knowledge-graph/overview`
- `GET /api/knowledge-graph/node/{node_id}`
- `POST /api/knowledge-graph/neighborhood`

## 后续增强方向

下一阶段不应替换当前图谱，而是在同一 schema 上增量增强：

1. 扩充口腔医学本体词表
   增加更细疾病、检查、治疗、病理机制、药物、材料、手术方式、适应证、禁忌证。

2. LLM 精抽关系
   从高质量教材和指南片段中抽取结构化关系，并追加 `extraction="llm"`、`confidence`、`evidence_span`。

3. 实体消歧
   合并同义词、简称、英文名和 OCR 变体，例如 RAU/复发性阿弗他溃疡/复发性口腔溃疡。

4. 学习智能体层
   为概念节点增加掌握度、先修依赖、题目、错题、复习计划和学习路径。

5. 图数据库导入
   当前 CSV/JSONL 可以导入 Neo4j；如果保持本地嵌入式，可以继续追加 Kuzu 导出脚本。
