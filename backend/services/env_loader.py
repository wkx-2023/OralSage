from __future__ import annotations

from pathlib import Path
import os


def load_project_env() -> None:
    project_root = Path(__file__).resolve().parents[2]
    for path in (project_root / ".env", project_root / "backend" / ".env"):
        if not path.exists():
            continue
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and value and key not in os.environ:
                os.environ[key] = value
