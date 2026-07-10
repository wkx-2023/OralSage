"""Rebuild formal Slides, Pin & Paper plan, and PPTX for one lesson."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.services.learning.memory import LearningMemory
from backend.services.learning.ppt_pipeline import refresh_ppt_resources


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session-id", required=True)
    parser.add_argument(
        "--db",
        default="data/learning/learning.sqlite3",
        help="Learning SQLite database containing the lesson outline.",
    )
    parser.add_argument(
        "--resource-root",
        default="data/learning/sessions",
        help="Session resource root containing handout/tutor caches.",
    )
    parser.add_argument("--no-export", action="store_true")
    args = parser.parse_args()

    memory = LearningMemory(db_path=Path(args.db))
    outline_record = memory.latest_outline(args.session_id)
    if not outline_record:
        raise SystemExit(f"outline not found for session {args.session_id}")

    result = refresh_ppt_resources(
        session_id=args.session_id,
        outline=outline_record["outline"],
        resource_root=Path(args.resource_root),
        export_pptx=not args.no_export,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result.get("pptx", {}).get("ok") is not True and not args.no_export:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
