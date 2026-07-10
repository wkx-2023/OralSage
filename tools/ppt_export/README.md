# OralSage PPTX exporter

Install the renderer dependency once:

```powershell
npm install --prefix tools/ppt_export
```

The learning orchestrator invokes `export.mjs` after every outline node has a
ready lecture script. The exporter reads `slides/slides.v1.json`, writes
`lesson.pptx`, and includes each page's narration in PowerPoint speaker notes.

Direct invocation:

```powershell
node tools/ppt_export/export.mjs <slides.v1.json> <lesson.pptx>
```

`pptx_status.json` is only marked `ready` after the backend validates the
generated OOXML package and slide count.
