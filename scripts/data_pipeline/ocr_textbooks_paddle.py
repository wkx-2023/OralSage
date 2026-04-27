from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

import fitz


PROJECT_ROOT = Path(__file__).resolve().parents[2]
TEXTBOOK_DIR = PROJECT_ROOT / "data" / "raw" / "textbooks"
OUT_DIR = PROJECT_ROOT / "data" / "processed" / "ocr_textbooks"
MANIFEST_PATH = OUT_DIR / "manifest.jsonl"
CACHE_DIR = PROJECT_ROOT / "models" / "paddleocr_cache"

os.environ.setdefault("PADDLE_PDX_CACHE_HOME", str(CACHE_DIR / "paddlex"))
os.environ.setdefault("PADDLE_HOME", str(CACHE_DIR / "paddle"))
os.environ.setdefault("MODELSCOPE_CACHE", str(CACHE_DIR / "modelscope"))
os.environ.setdefault("HF_HOME", str(CACHE_DIR / "huggingface"))
os.environ.setdefault("XDG_CACHE_HOME", str(CACHE_DIR / "xdg"))
os.environ.setdefault("FLAGS_use_mkldnn", "0")
os.environ.setdefault("FLAGS_use_onednn", "0")
os.environ.setdefault("FLAGS_enable_pir_api", "0")


def normalize_text(text: str) -> str:
    text = (text or "").replace("\x00", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def get_ocr() -> Any:
    import paddleocr.paddleocr as paddleocr_module
    from paddleocr import PaddleOCR

    if hasattr(paddleocr_module, "BASE_DIR"):
        paddleocr_module.BASE_DIR = str(CACHE_DIR / "paddleocr") + "/"

    try:
        return PaddleOCR(
            lang="ch",
            show_log=False,
            enable_mkldnn=False,
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_textline_orientation=False,
        )
    except TypeError:
        return PaddleOCR(lang="ch", use_angle_cls=False, show_log=False, enable_mkldnn=False)


def render_page(page: fitz.Page, dpi: int) -> Path:
    scale = dpi / 72
    pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
    image_path = OUT_DIR / "_page_cache" / f"page_{time.time_ns()}.jpg"
    image_path.parent.mkdir(parents=True, exist_ok=True)
    pix.save(str(image_path))
    return image_path


def extract_lines(result: Any) -> list[str]:
    lines: list[str] = []
    if not result:
        return lines

    # PaddleOCR 3.x predict() returns objects/dicts with rec_texts.
    for item in result if isinstance(result, list) else [result]:
        if isinstance(item, dict):
            texts = item.get("rec_texts") or item.get("texts")
            if isinstance(texts, list):
                lines.extend(str(text) for text in texts if str(text).strip())
                continue
        if hasattr(item, "json"):
            try:
                payload = item.json
                if callable(payload):
                    payload = payload()
                if isinstance(payload, dict):
                    texts = payload.get("rec_texts") or payload.get("texts")
                    if isinstance(texts, list):
                        lines.extend(str(text) for text in texts if str(text).strip())
                        continue
            except Exception:
                pass

        # PaddleOCR 2.x fallback: [[box, (text, score)], ...]
        if isinstance(item, list):
            for row in item:
                try:
                    text = row[1][0]
                except Exception:
                    continue
                if str(text).strip():
                    lines.append(str(text))
    return lines


def run_page_ocr(ocr: Any, image_path: Path) -> list[str]:
    if hasattr(ocr, "predict"):
        return extract_lines(ocr.predict(str(image_path)))
    return extract_lines(ocr.ocr(str(image_path), cls=True))


def write_page_record(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")


def process_pdf(
    ocr: Any,
    pdf_path: Path,
    *,
    dpi: int,
    start_page: int,
    max_pages: int | None,
    force: bool,
) -> dict[str, Any]:
    started = time.time()
    doc = fitz.open(str(pdf_path))
    safe_stem = re.sub(r"[^\w\u4e00-\u9fff.-]+", "_", pdf_path.stem)
    pdf_out = OUT_DIR / safe_stem
    pdf_out.mkdir(parents=True, exist_ok=True)

    total_pages = len(doc)
    first = max(1, start_page)
    last = total_pages if max_pages is None else min(total_pages, first + max_pages - 1)
    processed = 0
    skipped = 0
    errors: list[str] = []

    for page_no in range(first, last + 1):
        page_json = pdf_out / f"page_{page_no:04d}.json"
        if page_json.exists() and not force:
            skipped += 1
            continue

        image_path: Path | None = None
        try:
            image_path = render_page(doc[page_no - 1], dpi)
            lines = run_page_ocr(ocr, image_path)
            try:
                image_path.unlink(missing_ok=True)
            except Exception:
                pass
            text = normalize_text("\n".join(lines))
            write_page_record(
                page_json,
                {
                    "source": pdf_path.name,
                    "source_type": "textbook",
                    "title": f"{pdf_path.stem} 第{page_no}页",
                    "path": str(pdf_path.relative_to(PROJECT_ROOT)),
                    "page": page_no,
                    "text": text,
                    "ocr_engine": "paddleocr",
                    "dpi": dpi,
                    "line_count": len(lines),
                    "char_count": len(text),
                },
            )
            processed += 1
            if processed % 10 == 0:
                print(f"{pdf_path.name}: processed {processed} pages, current page {page_no}", flush=True)
        except Exception as exc:
            if image_path is not None:
                try:
                    image_path.unlink(missing_ok=True)
                except Exception:
                    pass
            message = f"{pdf_path.name} page {page_no}: {exc}"
            print("ERROR", message, file=sys.stderr, flush=True)
            errors.append(message)

    summary = {
        "source": pdf_path.name,
        "total_pages": total_pages,
        "page_from": first,
        "page_to": last,
        "processed": processed,
        "skipped": skipped,
        "errors": errors[:20],
        "seconds": round(time.time() - started, 2),
    }
    return summary


def append_manifest(summary: dict[str, Any]) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with MANIFEST_PATH.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(summary, ensure_ascii=False) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="OCR textbook PDFs with PaddleOCR into page JSON files.")
    parser.add_argument("--pdf", default=None, help="Only process PDFs whose filename contains this text.")
    parser.add_argument("--dpi", type=int, default=180)
    parser.add_argument("--start-page", type=int, default=1)
    parser.add_argument("--max-pages", type=int, default=None)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--limit-pdfs", type=int, default=None)
    args = parser.parse_args()

    pdfs = sorted(TEXTBOOK_DIR.glob("*.pdf"))
    if args.pdf:
        pdfs = [pdf for pdf in pdfs if args.pdf in pdf.name]
    if args.limit_pdfs:
        pdfs = pdfs[: args.limit_pdfs]
    if not pdfs:
        raise SystemExit("No textbook PDFs matched.")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    ocr = get_ocr()
    for pdf in pdfs:
        summary = process_pdf(
            ocr,
            pdf,
            dpi=args.dpi,
            start_page=args.start_page,
            max_pages=args.max_pages,
            force=args.force,
        )
        append_manifest(summary)
        print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
