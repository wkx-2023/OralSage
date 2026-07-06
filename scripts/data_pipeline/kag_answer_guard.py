from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

try:
    from .kag_solver import DEFAULT_RERANKER_MODEL_DIR, BGEReranker, text
except ImportError:
    from kag_solver import DEFAULT_RERANKER_MODEL_DIR, BGEReranker, text


SENTENCE_RE = re.compile(r"[^。！？!?；;\n]+[。！？!?；;]?")

SYSTEM_POLICY = """You are a grounded medical education answer generator.
Use only the supplied evidence units.
Every medical claim must cite at least one evidence_id.
Do not add dosage, contraindication, diagnostic threshold, treatment indication, or guideline recommendation unless it is explicitly supported by evidence.
If evidence is insufficient, say that the current evidence is insufficient.
Return strict JSON only."""


def split_claims(answer_text: str) -> list[str]:
    claims: list[str] = []
    for match in SENTENCE_RE.finditer(answer_text):
        claim = match.group(0).strip()
        if claim:
            claims.append(claim)
    return claims


def build_generation_prompt(retrieval_payload: dict[str, Any]) -> dict[str, Any]:
    grounded_context = retrieval_payload.get("grounded_context") or {}
    return {
        "system": SYSTEM_POLICY,
        "user_payload": {
            "query": retrieval_payload.get("query"),
            "task_type": retrieval_payload.get("task_type"),
            "generation_policy": grounded_context.get("generation_policy"),
            "evidence_units": grounded_context.get("evidence_units") or [],
            "do_not_infer": grounded_context.get("do_not_infer") or [],
            "output_schema": {
                "answer": "string",
                "claims": [
                    {
                        "claim_text": "string",
                        "supporting_evidence_ids": ["ev_001"],
                    }
                ],
                "unsupported": [
                    {
                        "claim_text": "string",
                        "reason": "insufficient_evidence|contradiction|not_medical_claim",
                    }
                ],
            },
        },
    }


class EvidenceSupportVerifier:
    def __init__(self, reranker_model_dir: Path = DEFAULT_RERANKER_MODEL_DIR, *, threshold: float = 0.0) -> None:
        self.threshold = threshold
        self.reranker = BGEReranker(reranker_model_dir, max_length=512, required=True)

    def verify(self, answer_text: str, evidence_units: list[dict[str, Any]]) -> dict[str, Any]:
        evidence_texts = [text(unit.get("text")) for unit in evidence_units]
        evidence_ids = [text(unit.get("evidence_id")) for unit in evidence_units]
        if not evidence_texts:
            return {
                "ok": False,
                "claims": [],
                "unsupported_claims": split_claims(answer_text),
                "reason": "no_evidence_units",
            }
        claim_results: list[dict[str, Any]] = []
        unsupported: list[dict[str, Any]] = []
        for claim in split_claims(answer_text):
            scores = self.reranker.score(claim, evidence_texts)
            best_index = max(range(len(scores)), key=lambda index: scores[index])
            best_score = float(scores[best_index])
            row = {
                "claim_text": claim,
                "supported": best_score >= self.threshold,
                "best_evidence_id": evidence_ids[best_index],
                "support_score": round(best_score, 6),
            }
            claim_results.append(row)
            if not row["supported"]:
                unsupported.append(row)
        return {
            "ok": not unsupported,
            "threshold": self.threshold,
            "claims": claim_results,
            "unsupported_claims": unsupported,
        }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build grounded prompt or verify answer support against KAG evidence.")
    parser.add_argument("--retrieval-payload", type=Path, required=True)
    parser.add_argument("--answer-file", type=Path)
    parser.add_argument("--reranker-model-dir", type=Path, default=DEFAULT_RERANKER_MODEL_DIR)
    parser.add_argument("--threshold", type=float, default=0.0)
    parser.add_argument("--mode", choices=["prompt", "verify"], default="prompt")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    payload = json.loads(args.retrieval_payload.read_text(encoding="utf-8"))
    if args.mode == "prompt":
        print(json.dumps(build_generation_prompt(payload), ensure_ascii=False, indent=2, sort_keys=True))
        return
    if not args.answer_file:
        raise ValueError("--answer-file is required in verify mode")
    answer_text = args.answer_file.read_text(encoding="utf-8")
    evidence_units = ((payload.get("grounded_context") or {}).get("evidence_units") or [])
    verifier = EvidenceSupportVerifier(args.reranker_model_dir, threshold=args.threshold)
    print(json.dumps(verifier.verify(answer_text, evidence_units), ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
