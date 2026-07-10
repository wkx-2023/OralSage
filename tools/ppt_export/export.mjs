import fs from "node:fs/promises";
import path from "node:path";
import PptxGenJS from "pptxgenjs";

const [, , manifestPath, outputPath] = process.argv;
if (!manifestPath || !outputPath) {
  throw new Error("usage: node export.mjs <manifest.json> <output.pptx>");
}

const manifest = JSON.parse(await fs.readFile(manifestPath, "utf8"));
const slides = Array.isArray(manifest.slides) ? manifest.slides : [];
if (!slides.length) {
  throw new Error("export manifest contains no slides");
}

const pptx = new PptxGenJS();
pptx.layout = "LAYOUT_WIDE";
pptx.author = "OralSage";
pptx.company = "OralSage";
pptx.subject = "Pin & Paper learning course";
pptx.title = manifest.title || "OralSage";
pptx.lang = "zh-CN";

for (const item of slides) {
  const imagePath = path.resolve(item.image_path || "");
  const slide = pptx.addSlide();
  slide.background = { color: "EFE56A" };
  slide.addImage({ path: imagePath, x: 0, y: 0, w: 13.333333, h: 7.5 });
  const notes = String(item.speaker_notes || "").trim();
  if (notes && typeof slide.addNotes === "function") {
    slide.addNotes(notes);
  }
}

await fs.mkdir(path.dirname(outputPath), { recursive: true });
await pptx.writeFile({ fileName: outputPath });
