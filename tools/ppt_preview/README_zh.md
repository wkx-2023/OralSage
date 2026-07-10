# PPT Preview 目录边界

本目录现在分成两类内容，后续开发必须先区分清楚。

## 正式模板候选池

正式 PPT 模板候选池只在：

```text
tools/ppt_preview/templates/
```

当前正式候选入口请看：

```text
tools/ppt_preview/templates/README_zh.md
```

新增视觉风格、增加 page_type、接真实 LLM、做浏览器验收，都应从 `templates/` 下的具体模板目录开始。

## Legacy 规则预览沙盒

以下内容属于早期 Python/规则自动排版预览沙盒，只保留作历史调试和回归参考：

```text
tools/ppt_preview/index.html
tools/ppt_preview/app.js
tools/ppt_preview/js/
tools/ppt_preview/styles/
tools/ppt_preview/styles.css
tools/ppt_preview/sample-slides.v1.json
tools/ppt_preview/stress-slides.v1.json
tools/ppt_preview/artifacts/
```

配套脚本也属于 legacy 预览沙盒：

```text
scripts/generate_ppt_preview_stress_fixtures.py
scripts/smoke_test_ppt_preview_assets.py
scripts/smoke_test_ppt_preview_render.py
```

这些文件不属于正式提交候选池，不作为新模板开发入口，也不应被新的模板任务文档引用为目标实现。

## 修改规则

- 不要把 legacy 规则预览沙盒接入正式模板候选池。
- 不要让新的 `llm-contract.md`、模板 README 或任务说明指向 legacy 预览沙盒。
- 不要用 legacy stress/sample fixture 证明真实讲义可以适配正式模板。
- 不要为了正式模板效果去修改 legacy 根预览器。
- 如果必须维护 legacy 预览器，只能把它当独立开发工具处理，不能影响 `templates/` 下的模板契约、planner 或真实 LLM 链路。

