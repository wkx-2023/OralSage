from __future__ import annotations

from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from fastapi.testclient import TestClient

from backend.main import app


def main() -> None:
    client = TestClient(app)
    response = client.post("/api/router/chat", json={"message": "我想学牙周炎"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["module"] == "learning_router_placeholder"
    assert payload["route"]["intent"] == "learning_router_rebuild"
    assert "学习型总控 agent 待重建" in payload["answer"]
    print("learning router placeholder smoke test passed")


if __name__ == "__main__":
    main()
