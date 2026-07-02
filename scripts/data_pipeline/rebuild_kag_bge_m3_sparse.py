from __future__ import annotations

import argparse
import gzip
import json
import math
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import jieba


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_KAG_DIR = PROJECT_ROOT / "data" / "kb" / "kag_bge_m3"
TOKEN_RE = re.compile(r"[A-Za-z0-9]+|[\u4e00-\u9fff]{1,}")


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def iter_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSONL at {path}:{line_no}: {exc}") from exc
            if isinstance(item, dict):
                yield item


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def tokenize(value: str) -> list[str]:
    tokens: list[str] = []
    for item in TOKEN_RE.findall(value.lower()):
        if re.fullmatch(r"[\u4e00-\u9fff]+", item):
            tokens.extend(token for token in jieba.cut(item) if len(token.strip()) >= 1)
            if len(item) >= 2:
                tokens.extend(item[index : index + 2] for index in range(len(item) - 1))
        else:
            tokens.append(item)
    return [token for token in tokens if token.strip()]


def rebuild(kag_dir: Path) -> dict[str, Any]:
    docs = list(iter_jsonl(kag_dir / "retrieval_corpus.jsonl"))
    df: Counter[str] = Counter()
    doc_terms: list[dict[str, Any]] = []
    postings: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for doc_index, doc in enumerate(docs):
        counts = Counter(tokenize(doc.get("text") or ""))
        doc_terms.append({"doc_id": doc["doc_id"], "length": sum(counts.values())})
        for term, tf in counts.items():
            df[term] += 1
            postings[term].append((doc_index, int(tf)))

    total_docs = len(docs)
    sparse_dir = kag_dir / "indexes" / "sparse"
    sparse_dir.mkdir(parents=True, exist_ok=True)
    index_payload = {
        "version": 1,
        "method": "jieba_bm25_compatible_inverted_index",
        "total_docs": total_docs,
        "avg_doc_len": round(sum(item["length"] for item in doc_terms) / max(total_docs, 1), 4),
        "doc_terms": doc_terms,
        "idf": {term: math.log((total_docs - freq + 0.5) / (freq + 0.5) + 1.0) for term, freq in df.items()},
        "postings": {term: pairs for term, pairs in postings.items()},
    }
    with gzip.open(sparse_dir / "inverted_index.json.gz", "wt", encoding="utf-8") as fh:
        json.dump(index_payload, fh, ensure_ascii=False, sort_keys=True)
    manifest = {
        "status": "ready",
        "method": index_payload["method"],
        "total_docs": total_docs,
        "terms": len(df),
        "index": "inverted_index.json.gz",
        "generated_at": now_iso(),
    }
    write_json(sparse_dir / "manifest.json", manifest)
    main_manifest_path = kag_dir / "manifest.json"
    if main_manifest_path.exists():
        main_manifest = json.loads(main_manifest_path.read_text(encoding="utf-8"))
        main_manifest.setdefault("indexes", {})["sparse"] = manifest
        main_manifest["updated_at"] = now_iso()
        write_json(main_manifest_path, main_manifest)
    return manifest


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Rebuild sparse BM25 index for the KAG BGE-M3 KB.")
    parser.add_argument("--kag-dir", type=Path, default=DEFAULT_KAG_DIR)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    manifest = rebuild(args.kag_dir)
    print(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
