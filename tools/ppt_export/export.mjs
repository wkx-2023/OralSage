import fs from "node:fs/promises";
import path from "node:path";
import PptxGenJS from "pptxgenjs";

const [, , inputPath, outputPath] = process.argv;
if (!inputPath || !outputPath) {
  throw new Error("usage: node export.mjs <slides.v1.json> <output.pptx>");
}

const deck = JSON.parse(await fs.readFile(inputPath, "utf8"));
const pptx = new PptxGenJS();
pptx.layout = "LAYOUT_WIDE";
pptx.author = "OralSage";
pptx.subject = deck.deckTitle || deck.deck_title || "OralSage learning course";
pptx.title = deck.deckTitle || deck.deck_title || "OralSage";
pptx.company = "OralSage";
pptx.lang = "zh-CN";
pptx.theme = {
  headFontFace: "Microsoft YaHei",
  bodyFontFace: "Microsoft YaHei",
  lang: "zh-CN",
};

const C = {
  ink: "172126",
  muted: "5D6B70",
  teal: "0F766E",
  tealLight: "DFF3F0",
  coral: "E76F51",
  coralLight: "FBE9E4",
  paper: "F7F9F8",
  white: "FFFFFF",
  line: "D9E2DF",
};

function addFooter(slide, section, page, total) {
  slide.addShape(pptx.ShapeType.line, {
    x: 0.6, y: 7.08, w: 12.1, h: 0,
    line: { color: C.line, width: 1 },
  });
  slide.addText(section || "OralSage", {
    x: 0.65, y: 7.12, w: 8.8, h: 0.18,
    fontFace: "Microsoft YaHei", fontSize: 8.5, color: C.muted,
    margin: 0, breakLine: false,
  });
  slide.addText(`${page} / ${total}`, {
    x: 11.1, y: 7.12, w: 1.5, h: 0.18,
    fontFace: "Aptos", fontSize: 8.5, color: C.muted,
    align: "right", margin: 0,
  });
}

function addCover() {
  const slide = pptx.addSlide();
  slide.background = { color: C.paper };
  slide.addShape(pptx.ShapeType.rect, {
    x: 0, y: 0, w: 0.18, h: 7.5,
    fill: { color: C.coral }, line: { color: C.coral },
  });
  slide.addText("ORALSAGE / 课程讲义", {
    x: 0.85, y: 0.75, w: 5.5, h: 0.3,
    fontFace: "Aptos", fontSize: 12, bold: true, color: C.teal,
    charSpacing: 0, margin: 0,
  });
  slide.addText(deck.deck_title || "OralSage 学习课程", {
    x: 0.85, y: 1.55, w: 10.9, h: 1.45,
    fontFace: "Microsoft YaHei", fontSize: 36, bold: true,
    color: C.ink, margin: 0, breakLine: false, valign: "mid",
  });
  slide.addText("循证讲义 · 逐页讲稿 · 同步语音", {
    x: 0.9, y: 3.35, w: 7.2, h: 0.45,
    fontFace: "Microsoft YaHei", fontSize: 16, color: C.muted, margin: 0,
  });
  slide.addShape(pptx.ShapeType.roundRect, {
    x: 9.75, y: 5.55, w: 2.35, h: 0.65, rectRadius: 0.04,
    fill: { color: C.tealLight }, line: { color: C.tealLight },
  });
  slide.addText(`${deck.expected_section_count || 0} 个章节`, {
    x: 9.75, y: 5.72, w: 2.35, h: 0.25,
    fontFace: "Microsoft YaHei", fontSize: 13, bold: true,
    color: C.teal, align: "center", margin: 0,
  });
}

function addContent(source, page, total) {
  const slide = pptx.addSlide();
  slide.background = { color: C.paper };
  const isClinical = source.slide_type === "clinical_example";
  const isConfusion = source.slide_type === "common_confusion";
  const accent = isClinical ? C.coral : C.teal;
  const accentLight = isClinical || isConfusion ? C.coralLight : C.tealLight;

  slide.addText(source.section_title || "课程内容", {
    x: 0.7, y: 0.42, w: 5.5, h: 0.28,
    fontFace: "Microsoft YaHei", fontSize: 10.5, bold: true,
    color: accent, margin: 0,
  });
  slide.addText(source.title || "未命名页面", {
    x: 0.7, y: 0.83, w: 11.7, h: 0.82,
    fontFace: "Microsoft YaHei", fontSize: 25, bold: true,
    color: C.ink, margin: 0, valign: "mid", breakLine: false,
  });
  slide.addShape(pptx.ShapeType.roundRect, {
    x: 0.7, y: 1.92, w: 11.92, h: 4.72,
    rectRadius: 0.03, fill: { color: C.white },
    line: { color: C.line, width: 1 }, shadow: { type: "outer", color: "CBD5D1", blur: 1, angle: 45, distance: 1, opacity: 0.12 },
  });
  slide.addShape(pptx.ShapeType.roundRect, {
    x: 0.98, y: 2.22, w: 0.12, h: 3.98,
    rectRadius: 0.02, fill: { color: accent }, line: { color: accent },
  });
  slide.addText((source.bullets || []).map((text) => ({
    text,
    options: { bullet: { indent: 18 }, hanging: 4, breakLine: true },
  })), {
    x: 1.35, y: 2.25, w: 10.55, h: 3.85,
    fontFace: "Microsoft YaHei", fontSize: 17, color: C.ink,
    breakLine: false, valign: "mid", margin: 0.08,
    paraSpaceAfterPt: 12, lineSpacingMultiple: 1.1,
  });
  const refs = source.evidence_refs || [];
  slide.addShape(pptx.ShapeType.roundRect, {
    x: 9.65, y: 6.22, w: 2.4, h: 0.28,
    rectRadius: 0.02, fill: { color: accentLight }, line: { color: accentLight },
  });
  slide.addText(refs.length ? `证据引用 ${refs.length} 条` : "讲义结构页", {
    x: 9.65, y: 6.285, w: 2.4, h: 0.14,
    fontFace: "Microsoft YaHei", fontSize: 8.5, color: accent,
    bold: true, align: "center", margin: 0,
  });
  addFooter(slide, source.section_title, page, total);
  const narration = source.narration_ref?.text || "";
  if (narration && typeof slide.addNotes === "function") {
    slide.addNotes(narration);
  }
}

addCover();
const slides = deck.slides || [];
slides.forEach((slide, index) => addContent(slide, index + 1, slides.length));
await fs.mkdir(path.dirname(outputPath), { recursive: true });
await pptx.writeFile({ fileName: outputPath });
