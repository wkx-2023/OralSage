"""Lightweight KAG path resolution for the learning agent.

Provides helpers to locate the KAG knowledge-base directory across the two
canonical layout variants (old ``data/kb/...`` and current ``data/data/kb/...``)
so that Planner and other learning modules don't need to hard-code paths.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


_REQUIRED_FILES = (
    "manifest.json",
    "chunks.jsonl",
    "retrieval_corpus.jsonl",
    "kg_nodes.jsonl",
    "kg_edges.jsonl",
    "questions.jsonl",
)


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def project_root() -> Path:
    """Return the repository root (three levels above this file)."""
    return Path(__file__).resolve().parents[3]


def candidate_kag_dirs() -> list[Path]:
    """Return candidate KAG directories in priority order.

    The first path matches the legacy ``configs/retrieval.yaml`` convention;
    the second is the current real-data location.
    """
    root = project_root()
    return [
        root / "data" / "kb" / "kag_bge_m3",          # old config path
        root / "data" / "data" / "kb" / "kag_bge_m3",  # current real path
    ]


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def resolve_kag_dir() -> Path:
    """Return the first candidate KAG directory that passes the file checklist.

    Raises :exc:`FileNotFoundError` when no candidate satisfies all checks.
    """
    missing: list[str] = []
    for cand in candidate_kag_dirs():
        if not cand.is_dir():
            missing.append(f"{cand} (not a directory)")
            continue
        unmet = [f for f in _REQUIRED_FILES if not (cand / f).is_file()]
        if not unmet:
            return cand
        missing.append(f"{cand} (missing: {', '.join(unmet)})")
    raise FileNotFoundError(
        "No valid KAG directory found. Checked candidates:\n- "
        + "\n- ".join(missing)
    )


# ---------------------------------------------------------------------------
# Manifest & summary
# ---------------------------------------------------------------------------


def load_kag_manifest(kag_dir: Path | None = None) -> dict[str, Any]:
    """Read ``manifest.json`` from *kag_dir* and return it as a dict.

    If *kag_dir* is *None*, calls :func:`resolve_kag_dir` first.
    """
    if kag_dir is None:
        kag_dir = resolve_kag_dir()
    with open(kag_dir / "manifest.json", "r", encoding="utf-8") as fh:
        return json.load(fh)


def kag_summary(kag_dir: Path | None = None) -> dict[str, Any]:
    """Return a terminal-friendly summary of the KAG knowledge base.

    Parameters
    ----------
    kag_dir:
        Path to the KAG directory.  If *None*, resolved automatically.

    Returns
    -------
    dict
        Keys: ``kag_dir``, ``ok``, ``manifest_ok``, ``chunks``, ``kg_nodes``,
        ``kg_edges``, ``questions``, ``retrieval_docs``, ``dense_index_exists``,
        ``sparse_index_exists``, ``graph_index_exists``.
    """
    if kag_dir is None:
        kag_dir = resolve_kag_dir()

    kag_dir = Path(kag_dir)
    result: dict[str, Any] = {
        "kag_dir": str(kag_dir),
        "ok": False,
        "manifest_ok": None,
        "chunks": None,
        "kg_nodes": None,
        "kg_edges": None,
        "questions": None,
        "retrieval_docs": None,
        "dense_index_exists": False,
        "sparse_index_exists": False,
        "graph_index_exists": False,
    }

    # --- manifest fields (robust to both "counts" and top-level layouts) ---
    try:
        manifest = load_kag_manifest(kag_dir)
        result["manifest_ok"] = True

        # prefer counts sub-key, fall back to top-level keys
        counts = manifest.get("counts", {}) if isinstance(manifest, dict) else {}
        result["chunks"] = counts.get("chunks", manifest.get("chunks"))
        result["kg_nodes"] = counts.get("kg_nodes", manifest.get("kg_nodes"))
        result["kg_edges"] = counts.get("kg_edges", manifest.get("kg_edges"))
        result["questions"] = counts.get("questions", manifest.get("questions"))
        result["retrieval_docs"] = counts.get(
            "retrieval_docs", manifest.get("retrieval_docs")
        )
    except Exception:
        result["manifest_ok"] = False

    # --- index directories ---
    indexes_dir = kag_dir / "indexes"
    if indexes_dir.is_dir():
        result["dense_index_exists"] = (indexes_dir / "dense_bge_m3").is_dir()
        result["sparse_index_exists"] = (indexes_dir / "sparse").is_dir()
        result["graph_index_exists"] = (indexes_dir / "graph").is_dir()

    # --- overall ok ---
    result["ok"] = bool(
        result["manifest_ok"]
        and result["chunks"] is not None
        and result["kg_nodes"] is not None
    )

    return result
