from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CANONICAL_DIR = PROJECT_ROOT / "data" / "kb" / "canonical"


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def iter_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            item = json.loads(line)
            if isinstance(item, dict):
                yield item


def count_jsonl(path: Path) -> int:
    if not path.exists():
        return 0
    count = 0
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                count += 1
    return count


def update(canonical_dir: Path) -> dict[str, Any]:
    manifest_path = canonical_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    counts = dict(manifest.get("counts") or {})
    for name in ("sources", "nodes", "edges", "chunks", "questions", "question_links", "alignments", "clinical_rules"):
        counts[name] = count_jsonl(canonical_dir / f"{name}.jsonl")

    alignments = list(iter_jsonl(canonical_dir / "alignments.jsonl"))
    counts["alignments_by_status"] = dict(Counter(item.get("verification_status") for item in alignments))
    counts["alignments_by_relation"] = dict(Counter(item.get("relation") for item in alignments))
    counts["alignments_by_method"] = dict(Counter("|".join(item.get("method_chain") or []) for item in alignments))
    manifest["counts"] = counts
    manifest["updated_at"] = now_iso()
    validation_path = canonical_dir / "validation_report.json"
    if validation_path.exists():
        validation = json.loads(validation_path.read_text(encoding="utf-8"))
        manifest["validation"] = {
            "ok": bool(validation.get("ok")),
            "error_count": len(validation.get("errors") or []),
            "report": "data/kb/canonical/validation_report.json",
        }
    manifest.setdefault("quality_gates", {})["kg_chunk_alignment_status"] = (
        "verified_exact_quote_ready" if counts.get("alignments") else "not_started"
    )
    if manifest.get("validation", {}).get("ok"):
        manifest.setdefault("quality_gates", {})["canonical_integrity"] = "passed"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return manifest


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Refresh canonical KB manifest counts after downstream stages.")
    parser.add_argument("--canonical-dir", type=Path, default=DEFAULT_CANONICAL_DIR)
    return parser.parse_args()


def main() -> None:
    manifest = update(parse_args().canonical_dir)
    print(json.dumps({"counts": manifest.get("counts"), "quality_gates": manifest.get("quality_gates")}, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
