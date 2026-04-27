from __future__ import annotations

from backend.services.env_loader import load_project_env


def load_literature_env() -> None:
    load_project_env()
