from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_KAG_DIR = PROJECT_ROOT / "data" / "kb" / "kag_bge_m3"


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


def rel_path(path: Path) -> str:
    try:
        return path.resolve().relative_to(PROJECT_ROOT.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def sync(kag_dir: Path) -> dict[str, Any]:
    dense_dir = kag_dir / "indexes" / "dense_bge_m3"
    metadata_path = dense_dir / "doc_metadata.json"
    if not metadata_path.exists():
        raise FileNotFoundError(metadata_path)

    previous = json.loads(metadata_path.read_text(encoding="utf-8"))
    previous_doc_ids = previous.get("doc_ids") or []
    docs = list(iter_jsonl(kag_dir / "retrieval_corpus.jsonl"))
    doc_ids = [doc["doc_id"] for doc in docs]
    if previous_doc_ids and previous_doc_ids != doc_ids:
        raise RuntimeError("Dense doc_id order does not match retrieval_corpus; vectors must be rebuilt, not metadata-synced.")

    metadata = [
        {
            "doc_id": doc.get("doc_id"),
            "doc_type": doc.get("doc_type"),
            "source_layer": doc.get("source_layer"),
            "authority_level": doc.get("authority_level"),
            "metadata": doc.get("metadata") or {},
        }
        for doc in docs
    ]
    payload = {
        "doc_ids": doc_ids,
        "metadata": metadata,
        "metadata_synced_at": now_iso(),
        "metadata_source": rel_path(kag_dir / "retrieval_corpus.jsonl"),
        "vectors_unchanged": True,
    }
    write_json(metadata_path, payload)
    report = {
        "generated_at": now_iso(),
        "doc_metadata": rel_path(metadata_path),
        "retrieval_docs": len(docs),
        "doc_ids_order_preserved": True,
        "vectors_unchanged": True,
    }
    write_json(dense_dir / "metadata_sync_report.json", report)
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sync dense doc_metadata.json from retrieval_corpus metadata.")
    parser.add_argument("--kag-dir", type=Path, default=DEFAULT_KAG_DIR)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    report = sync(args.kag_dir)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
