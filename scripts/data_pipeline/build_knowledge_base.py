from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Iterable

import pandas as pd
from pypdf import PdfReader


PROJECT_ROOT = Path(__file__).resolve().parents[2]
RAW_DIR = PROJECT_ROOT / "data" / "raw"
OUT_DIR = PROJECT_ROOT / "data" / "processed" / "knowledge_base"
OCR_TEXTBOOK_DIR = PROJECT_ROOT / "data" / "processed" / "ocr_textbooks"


def normalize_text(text: str) -> str:
    text = (text or "").replace("\x00", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = text.replace("本俗料", "本资料").replace("本咨料", "本资料")
    text = text.replace("口照", "口腔").replace("口配", "口腔")
    text = text.replace("教料", "教材").replace("救材", "教材")
    text = text.replace("医字", "医学").replace("上生", "卫生")
    text = text.replace("用十", "用于").replace("用干", "用于")
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def text_quality(text: str) -> dict[str, Any]:
    text = normalize_text(text)
    if not text:
        return {"score": 0.0, "chinese_ratio": 0.0, "suspicious_ratio": 1.0}
    visible = [ch for ch in text if not ch.isspace()]
    chinese = sum(1 for ch in visible if "\u4e00" <= ch <= "\u9fff")
    suspicious_chars = set("□■�▯")
    suspicious = sum(1 for ch in visible if ch in suspicious_chars)
    common_noise = len(re.findall(r"(口照|口配|医字|救材|教料|本俗料|用十|用干)", text))
    chinese_ratio = chinese / max(len(visible), 1)
    suspicious_ratio = (suspicious + common_noise * 8) / max(len(visible), 1)
    score = max(0.0, min(1.0, chinese_ratio * 1.15 - suspicious_ratio * 2.2))
    return {
        "score": round(score, 4),
        "chinese_ratio": round(chinese_ratio, 4),
        "suspicious_ratio": round(suspicious_ratio, 4),
        "noise_hits": common_noise,
    }


def stable_id(*parts: str) -> str:
    digest = hashlib.sha1("||".join(parts).encode("utf-8", errors="ignore")).hexdigest()
    return digest[:16]


def chunk_text(text: str, target_chars: int = 900, overlap_chars: int = 120) -> list[str]:
    text = normalize_text(text)
    if not text:
        return []
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]
    chunks: list[str] = []
    current = ""

    for para in paragraphs:
        if len(current) + len(para) + 2 <= target_chars:
            current = f"{current}\n\n{para}".strip()
            continue
        if current:
            chunks.append(current)
        if len(para) <= target_chars:
            current = para
            continue
        start = 0
        while start < len(para):
            end = min(len(para), start + target_chars)
            chunks.append(para[start:end].strip())
            if end >= len(para):
                break
            start = max(end - overlap_chars, start + 1)
        current = ""

    if current:
        chunks.append(current)
    return chunks


def write_jsonl(path: Path, records: Iterable[dict[str, Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        for record in records:
            text = normalize_text(str(record.get("text") or ""))
            if len(text) < 40:
                continue
            record["text"] = text
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            count += 1
    return count


def build_merck(limit: int | None = None) -> int:
    src = RAW_DIR / "merck_manual" / "msd_manual.jsonl"
    out = OUT_DIR / "merck_manual.jsonl"
    records: list[dict[str, Any]] = []
    if not src.exists():
        return write_jsonl(out, records)

    fields = ("introduction", "symptoms_and_signs", "diagnosis", "treatment", "key_points")
    with src.open("r", encoding="utf-8") as fh:
        for row_index, line in enumerate(fh):
            if limit and row_index >= limit:
                break
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            title = str(row.get("title") or "默沙东诊疗手册")
            body = "\n\n".join(str(row.get(field) or "").strip() for field in fields if str(row.get(field) or "").strip())
            for chunk_index, chunk in enumerate(chunk_text(body)):
                records.append(
                    {
                        "id": stable_id("merck", title, str(row_index), str(chunk_index)),
                        "source": "默沙东诊疗手册",
                        "source_type": "general_reference",
                        "title": title,
                        "path": str(src.relative_to(PROJECT_ROOT)),
                        "text": chunk,
                        "metadata": {"row_index": row_index, "chunk_index": chunk_index},
                    }
                )
    return write_jsonl(out, records)


def _extract_pdf_pages(path: Path, max_pages: int | None = None) -> Iterable[tuple[int, str]]:
    reader = PdfReader(str(path))
    total = len(reader.pages)
    page_count = min(total, max_pages) if max_pages else total
    for index in range(page_count):
        try:
            text = reader.pages[index].extract_text() or ""
        except Exception:
            text = ""
        text = normalize_text(text)
        if text:
            yield index + 1, text


def build_pdf_dir(dirname: str, output_name: str, source_type: str, max_pages: int | None = None) -> int:
    src_dir = RAW_DIR / dirname
    out = OUT_DIR / output_name
    records: list[dict[str, Any]] = []
    if not src_dir.exists():
        return write_jsonl(out, records)

    for pdf in sorted(src_dir.glob("*.pdf")):
        title = pdf.stem
        try:
            pages = list(_extract_pdf_pages(pdf, max_pages=max_pages))
        except Exception as exc:
            print(f"skip pdf: {pdf.name}: {exc}")
            continue
        for page, text in pages:
            heading = f"{title} 第{page}页"
            for chunk_index, chunk in enumerate(chunk_text(text)):
                quality = text_quality(chunk)
                records.append(
                    {
                        "id": stable_id(source_type, pdf.name, str(page), str(chunk_index)),
                        "source": pdf.name,
                        "source_type": source_type,
                        "title": heading,
                        "path": str(pdf.relative_to(PROJECT_ROOT)),
                        "text": chunk,
                        "metadata": {"page": page, "chunk_index": chunk_index, "ocr_quality": quality},
                    }
                )
    return write_jsonl(out, records)


def build_textbooks_from_ocr(max_pages: int | None = None) -> int:
    out = OUT_DIR / "oral_textbooks.jsonl"
    records: list[dict[str, Any]] = []
    page_files = sorted(
        path
        for path in OCR_TEXTBOOK_DIR.rglob("page_*.json")
        if "_page_cache" not in path.parts
    )
    if max_pages:
        page_files = page_files[:max_pages]

    for page_file in page_files:
        try:
            page = json.loads(page_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        text = normalize_text(str(page.get("text") or ""))
        if not text:
            continue
        source = str(page.get("source") or page_file.parent.name)
        title = str(page.get("title") or f"{source} 第{page.get('page', '')}页")
        for chunk_index, chunk in enumerate(chunk_text(text)):
            quality = text_quality(chunk)
            records.append(
                {
                    "id": stable_id("textbook_ocr", source, str(page.get("page", "")), str(chunk_index), chunk[:80]),
                    "source": source,
                    "source_type": "textbook",
                    "title": title,
                    "path": str(page.get("path") or page_file.relative_to(PROJECT_ROOT)),
                    "text": chunk,
                    "metadata": {
                        "page": page.get("page"),
                        "chunk_index": chunk_index,
                        "ocr_engine": page.get("ocr_engine", "paddleocr"),
                        "ocr_json": str(page_file.relative_to(PROJECT_ROOT)),
                        "ocr_quality": quality,
                    },
                }
            )
    return write_jsonl(out, records)


def build_clinical_cases(max_rows: int | None = None) -> int:
    src_dir = RAW_DIR / "clinical_cases"
    out = OUT_DIR / "clinical_cases.jsonl"
    records: list[dict[str, Any]] = []
    files = list(src_dir.glob("*.xls*")) if src_dir.exists() else []

    for file in files:
        try:
            sheets = pd.read_excel(file, sheet_name=None)
        except Exception as exc:
            print(f"skip xls: {file.name}: {exc}")
            continue
        for sheet_name, frame in sheets.items():
            frame = frame.fillna("")
            for row_index, row in frame.iterrows():
                if max_rows and len(records) >= max_rows:
                    break
                pairs = []
                for col, val in row.items():
                    value = str(val).strip()
                    if value:
                        pairs.append(f"{col}：{value}")
                text = "\n".join(pairs)
                if not text:
                    continue
                diagnosis = next((str(row[col]).strip() for col in frame.columns if "诊断" in str(col) and str(row[col]).strip()), "")
                records.append(
                    {
                        "id": stable_id("clinical_case", file.name, sheet_name, str(row_index)),
                        "source": file.name,
                        "source_type": "clinical_case",
                        "title": diagnosis or f"{sheet_name} 第{row_index + 1}行",
                        "path": str(file.relative_to(PROJECT_ROOT)),
                        "text": text,
                        "metadata": {"sheet": str(sheet_name), "row_index": int(row_index)},
                    }
                )
            if max_rows and len(records) >= max_rows:
                break
    return write_jsonl(out, records)


def main() -> None:
    parser = argparse.ArgumentParser(description="Build local JSONL knowledge-base chunks for OralPath.")
    parser.add_argument("--skip-textbooks", action="store_true", help="Skip textbook PDFs for a faster first build.")
    parser.add_argument("--only-textbooks", action="store_true", help="Only build textbook chunks.")
    parser.add_argument(
        "--textbook-source",
        choices=("auto", "ocr", "pdf"),
        default="auto",
        help="Use OCR page JSONs, PDF text layer, or auto-prefer OCR for textbooks.",
    )
    parser.add_argument("--max-pdf-pages", type=int, default=None, help="Limit pages per PDF during quick experiments.")
    parser.add_argument("--limit-merck", type=int, default=None, help="Limit MSD manual rows during quick experiments.")
    parser.add_argument("--limit-cases", type=int, default=None, help="Limit clinical case rows during quick experiments.")
    args = parser.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    counts = {}
    if not args.only_textbooks:
        counts.update(
            {
                "clinical_norms": build_pdf_dir("guidelines", "clinical_norms.jsonl", "guideline", args.max_pdf_pages),
                "merck_manual": build_merck(args.limit_merck),
                "clinical_cases": build_clinical_cases(args.limit_cases),
            }
        )
    if not args.skip_textbooks or args.only_textbooks:
        if args.textbook_source in {"auto", "ocr"} and any(OCR_TEXTBOOK_DIR.rglob("page_*.json")):
            counts["oral_textbooks"] = build_textbooks_from_ocr(args.max_pdf_pages)
            counts["oral_textbooks_source"] = "paddleocr"
        elif args.textbook_source == "ocr":
            counts["oral_textbooks"] = 0
            counts["oral_textbooks_source"] = "paddleocr_missing"
        else:
            counts["oral_textbooks"] = build_pdf_dir("textbooks", "oral_textbooks.jsonl", "textbook", args.max_pdf_pages)
            counts["oral_textbooks_source"] = "pdf_text_layer"
    elif not args.only_textbooks:
        counts["oral_textbooks"] = 0

    print(json.dumps(counts, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
