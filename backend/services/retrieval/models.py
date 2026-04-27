from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class EvidenceChunk:
    id: str
    text: str
    source: str
    source_type: str
    title: str = ""
    path: str = ""
    score: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

    def excerpt(self, limit: int = 360) -> str:
        compact = " ".join((self.text or "").split())
        if len(compact) <= limit:
            return compact
        return compact[: limit - 3].rstrip() + "..."

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source": self.source,
            "source_type": self.source_type,
            "title": self.title,
            "excerpt": self.excerpt(),
            "score": round(float(self.score), 4),
            "path": self.path,
            "metadata": self.metadata,
        }
