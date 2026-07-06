from __future__ import annotations

import argparse
import gzip
import hashlib
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
DEFAULT_RERANKER_MODEL_DIR = PROJECT_ROOT / "models" / "rerankers" / "bge-reranker-v2-m3"
TOKEN_RE = re.compile(r"[A-Za-z0-9]+|[\u4e00-\u9fff]+")
DOMAIN_RELATIONS = [
    "supports",
    "refines",
    "guideline_for",
    "diagnosis_criterion_for",
    "treatment_for",
    "contraindication_for",
    "risk_factor_for",
    "adverse_effect_of",
    "prognosis_for",
    "external_reference_for",
    "conflicts_with",
]


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


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True))
            fh.write("\n")
            count += 1
    return count


def text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value).strip()


def as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, set):
        return list(value)
    return [value]


def stable_id(prefix: str, *parts: Any) -> str:
    digest = hashlib.sha1()
    for part in parts:
        digest.update(text(part).encode("utf-8", errors="ignore"))
        digest.update(b"\0")
    return f"{prefix}:{digest.hexdigest()[:24]}"


def compact_text(*values: Any, limit: int | None = None) -> str:
    chunks: list[str] = []
    for value in values:
        if isinstance(value, list):
            chunks.extend(text(item) for item in value if text(item))
        else:
            body = text(value)
            if body:
                chunks.append(body)
    body = re.sub(r"\s+", " ", "\n".join(chunks)).strip()
    return body[:limit] if limit else body


def tokenize(value: str) -> list[str]:
    tokens: list[str] = []
    for item in TOKEN_RE.findall(value.lower()):
        if re.fullmatch(r"[\u4e00-\u9fff]+", item):
            tokens.extend(token for token in jieba.cut(item) if token.strip())
            if len(item) >= 2:
                tokens.extend(item[index : index + 2] for index in range(len(item) - 1))
        else:
            tokens.append(item)
    return [token for token in tokens if token.strip()]


class LocalReranker:
    def __init__(self, model_dir: Path, *, max_length: int, required: bool) -> None:
        self.available = False
        self.max_length = max_length
        if not model_dir.exists():
            if required:
                raise FileNotFoundError(f"bge-reranker-v2-m3 model is missing: {model_dir}")
            self._score_pairs = None
            return
        import torch
        from transformers import AutoModelForSequenceClassification, AutoTokenizer

        tokenizer = AutoTokenizer.from_pretrained(str(model_dir), local_files_only=True)
        model = AutoModelForSequenceClassification.from_pretrained(str(model_dir), local_files_only=True)
        model.eval()
        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        model.to(device)

        def score_pairs(query: str, docs: list[str]) -> list[float]:
            scores: list[float] = []
            with torch.no_grad():
                for doc in docs:
                    encoded = tokenizer(
                        query,
                        doc,
                        padding=True,
                        truncation=True,
                        max_length=max_length,
                        return_tensors="pt",
                    )
                    encoded = {key: value.to(device) for key, value in encoded.items()}
                    scores.append(float(model(**encoded).logits.detach().float().view(-1)[0].cpu().item()))
            return scores

        self._score_pairs = score_pairs
        self.available = True

    def score(self, query: str, docs: list[str]) -> list[float]:
        if not self._score_pairs:
            raise RuntimeError("Reranker is not available.")
        return self._score_pairs(query, docs)


def build_node_profiles(kag_dir: Path, *, evidence_snippet_chars: int) -> list[dict[str, Any]]:
    nodes = list(iter_jsonl(kag_dir / "kg_nodes.jsonl"))
    chunks = list(iter_jsonl(kag_dir / "chunks.jsonl"))
    snippets_by_node: dict[str, list[str]] = defaultdict(list)
    for chunk in chunks:
        if chunk.get("source_layer") != "textbook_evidence":
            continue
        snippet = text(chunk.get("text"))[:evidence_snippet_chars]
        if not snippet:
            continue
        for node_id in as_list(chunk.get("core_node_ids")):
            node_id = text(node_id)
            if node_id and len(snippets_by_node[node_id]) < 3:
                snippets_by_node[node_id].append(snippet)
    profiles: list[dict[str, Any]] = []
    for node in nodes:
        node_id = text(node.get("node_id"))
        if not node_id:
            continue
        aliases = [text(item) for item in as_list(node.get("aliases")) if text(item)]
        evidence = snippets_by_node.get(node_id, [])
        profile_text = compact_text(
            node.get("canonical_name"),
            aliases,
            node.get("node_type"),
            node.get("node_kind"),
            node.get("subject"),
            node.get("chapter"),
            node.get("section"),
            evidence,
            limit=3000,
        )
        profiles.append(
            {
                "node_id": node_id,
                "canonical_name": node.get("canonical_name"),
                "aliases": aliases,
                "node_type": node.get("node_type"),
                "node_kind": node.get("node_kind"),
                "subject": node.get("subject"),
                "chapter": node.get("chapter"),
                "section": node.get("section"),
                "text": profile_text,
                "evidence_snippet_count": len(evidence),
            }
        )
    return profiles


def write_profile_bm25(kag_dir: Path, profiles: list[dict[str, Any]]) -> dict[str, Any]:
    df: Counter[str] = Counter()
    doc_terms: list[dict[str, Any]] = []
    postings: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for doc_index, profile in enumerate(profiles):
        counts = Counter(tokenize(profile.get("text") or ""))
        doc_terms.append({"node_id": profile["node_id"], "length": sum(counts.values())})
        for term, tf in counts.items():
            df[term] += 1
            postings[term].append((doc_index, int(tf)))
    total_docs = len(profiles)
    payload = {
        "version": 1,
        "method": "kg_node_profile_bm25",
        "k1": 1.5,
        "b": 0.75,
        "total_docs": total_docs,
        "avg_doc_len": round(sum(item["length"] for item in doc_terms) / max(total_docs, 1), 4),
        "doc_terms": doc_terms,
        "idf": {term: math.log((total_docs - freq + 0.5) / (freq + 0.5) + 1.0) for term, freq in df.items()},
        "postings": {term: pairs for term, pairs in postings.items()},
    }
    out_dir = kag_dir / "indexes" / "domain_alignment" / "node_profiles"
    out_dir.mkdir(parents=True, exist_ok=True)
    with gzip.open(out_dir / "bm25.json.gz", "wt", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, sort_keys=True)
    return {
        "status": "ready",
        "method": payload["method"],
        "terms": len(df),
        "total_docs": total_docs,
        "path": "indexes/domain_alignment/node_profiles/bm25.json.gz",
    }


def build_alias_map(profiles: list[dict[str, Any]]) -> dict[str, list[str]]:
    alias_map: dict[str, list[str]] = defaultdict(list)
    for profile in profiles:
        for name in [profile.get("canonical_name"), *as_list(profile.get("aliases"))]:
            alias = text(name).lower()
            if len(alias) >= 2 and profile["node_id"] not in alias_map[alias]:
                alias_map[alias].append(profile["node_id"])
    return dict(alias_map)


def extract_domain_mentions(chunks: Iterable[dict[str, Any]], alias_map: dict[str, list[str]], *, max_mentions_per_chunk: int) -> list[dict[str, Any]]:
    mentions: list[dict[str, Any]] = []
    aliases = sorted(alias_map.keys(), key=len, reverse=True)
    per_chunk: Counter[str] = Counter()
    for chunk in chunks:
        if chunk.get("source_layer") not in {"clinical_authority", "external_reference"}:
            continue
        chunk_id = text(chunk.get("chunk_id"))
        body = text(chunk.get("text"))
        if not chunk_id or not body:
            continue
        body_lc = body.lower()
        seen_aliases: set[str] = set()
        for alias in aliases:
            if per_chunk[chunk_id] >= max_mentions_per_chunk:
                break
            if alias in seen_aliases or alias not in body_lc:
                continue
            seen_aliases.add(alias)
            start = body_lc.find(alias)
            window = body[max(0, start - 300) : min(len(body), start + len(alias) + 500)]
            mentions.append(
                {
                    "mention_id": stable_id("domain_mention", chunk_id, alias, start),
                    "chunk_id": chunk_id,
                    "source_layer": chunk.get("source_layer"),
                    "source_type": chunk.get("source_type"),
                    "source_title": chunk.get("source_title"),
                    "mention": alias,
                    "claim_text": window,
                    "candidate_node_ids_from_alias": alias_map.get(alias, []),
                    "extraction_method": "kg_alias_exact_in_domain_chunk",
                }
            )
            per_chunk[chunk_id] += 1
    return mentions


def bm25_profile_search(query: str, index: dict[str, Any], profiles: list[dict[str, Any]], *, top_k: int) -> list[dict[str, Any]]:
    query_terms = Counter(tokenize(query))
    if not query_terms:
        return []
    avg_len = float(index.get("avg_doc_len") or 1.0)
    idf = index["idf"]
    postings = index["postings"]
    doc_terms = index["doc_terms"]
    k1 = float(index.get("k1") or 1.5)
    b = float(index.get("b") or 0.75)
    scores: dict[int, float] = defaultdict(float)
    for term, qtf in query_terms.items():
        term_idf = float(idf.get(term) or 0.0)
        if term_idf <= 0:
            continue
        for doc_index, tf in postings.get(term, []):
            doc_len = max(float(doc_terms[doc_index].get("length") or 1.0), 1.0)
            denom = tf + k1 * (1 - b + b * doc_len / avg_len)
            scores[doc_index] += qtf * term_idf * (tf * (k1 + 1)) / denom
    ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)[:top_k]
    return [{"node_id": profiles[index]["node_id"], "score": round(score, 6), "method": "bm25_node_profile"} for index, score in ranked]


def relation_typing_task(candidate: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
    return {
        "task": "domain_relation_typing",
        "instruction": "Choose exactly one relation. Return strict JSON. Use only claim_text and node_profile.",
        "allowed_relations": DOMAIN_RELATIONS,
        "input": {
            "chunk_id": candidate.get("chunk_id"),
            "node_id": candidate.get("node_id"),
            "mention": candidate.get("mention"),
            "claim_text": text(candidate.get("claim_text"))[:1200],
            "node_profile": {
                "canonical_name": profile.get("canonical_name"),
                "aliases": profile.get("aliases"),
                "node_type": profile.get("node_type"),
                "subject": profile.get("subject"),
                "chapter": profile.get("chapter"),
                "section": profile.get("section"),
            },
        },
        "output_schema": {
            "relation": "one of allowed_relations",
            "evidence_text": "short excerpt copied from claim_text",
            "rationale": "brief reason grounded in claim_text",
            "confidence": "high|medium|low",
        },
    }


def build_candidates(mentions: list[dict[str, Any]], profiles: list[dict[str, Any]], bm25_index: dict[str, Any], *, top_k: int) -> list[dict[str, Any]]:
    profile_by_id = {profile["node_id"]: profile for profile in profiles}
    candidates: list[dict[str, Any]] = []
    for mention in mentions:
        query = compact_text(mention.get("mention"), mention.get("claim_text"), limit=1200)
        merged: dict[str, dict[str, Any]] = {}
        for node_id in as_list(mention.get("candidate_node_ids_from_alias")):
            if node_id in profile_by_id:
                merged[node_id] = {"node_id": node_id, "methods": ["alias_exact"], "scores": {"alias_exact": 1.0}}
        for hit in bm25_profile_search(query, bm25_index, profiles, top_k=top_k):
            row = merged.setdefault(hit["node_id"], {"node_id": hit["node_id"], "methods": [], "scores": {}})
            row["methods"].append(hit["method"])
            row["scores"][hit["method"]] = hit["score"]
        for candidate in merged.values():
            profile = profile_by_id[candidate["node_id"]]
            candidates.append(
                {
                    "candidate_id": stable_id("domain_candidate", mention["mention_id"], candidate["node_id"]),
                    "mention_id": mention["mention_id"],
                    "chunk_id": mention["chunk_id"],
                    "node_id": candidate["node_id"],
                    "mention": mention["mention"],
                    "claim_text": mention["claim_text"],
                    "source_layer": mention.get("source_layer"),
                    "candidate_methods": sorted(set(candidate["methods"])),
                    "candidate_scores": candidate["scores"],
                    "node_profile_text": profile.get("text"),
                }
            )
    return candidates


def finalize_alignments(
    candidates: list[dict[str, Any]],
    profiles: list[dict[str, Any]],
    *,
    reranker: LocalReranker,
    top1_threshold: float,
    margin_threshold: float,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    profile_by_id = {profile["node_id"]: profile for profile in profiles}
    by_mention: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for candidate in candidates:
        by_mention[candidate["mention_id"]].append(candidate)
    edges: list[dict[str, Any]] = []
    review: list[dict[str, Any]] = []
    for _, group in by_mention.items():
        query = compact_text(group[0].get("mention"), group[0].get("claim_text"), limit=1200)
        if not reranker.available:
            review.extend({**item, "review_reason": "reranker_missing"} for item in group)
            continue
        scores = reranker.score(query, [text(item.get("node_profile_text")) for item in group])
        ranked = sorted(zip(group, scores), key=lambda item: item[1], reverse=True)
        top_item, top_score = ranked[0]
        second_score = ranked[1][1] if len(ranked) > 1 else -999.0
        margin = float(top_score - second_score)
        profile = profile_by_id[top_item["node_id"]]
        if top_score < top1_threshold or margin < margin_threshold:
            review.extend(
                {
                    **item,
                    "reranker_score": round(float(score), 6),
                    "top1_score": round(float(top_score), 6),
                    "top1_top2_margin": round(margin, 6),
                    "review_reason": "low_alignment_confidence",
                }
                for item, score in ranked
            )
            continue
        review.append(
            {
                **top_item,
                "reranker_score": round(float(top_score), 6),
                "top1_top2_margin": round(margin, 6),
                "review_reason": "relation_typing_required",
                "relation_typing_task": relation_typing_task(top_item, profile),
            }
        )
    return edges, review


def load_bm25_index(path: Path) -> dict[str, Any]:
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        return json.load(fh)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build clinical/Merck to KG alignment artifacts.")
    parser.add_argument("--kag-dir", type=Path, default=DEFAULT_KAG_DIR)
    parser.add_argument("--reranker-model-dir", type=Path, default=DEFAULT_RERANKER_MODEL_DIR)
    parser.add_argument("--max-mentions-per-chunk", type=int, default=8)
    parser.add_argument("--candidate-top-k", type=int, default=12)
    parser.add_argument("--evidence-snippet-chars", type=int, default=360)
    parser.add_argument("--reranker-max-length", type=int, default=512)
    parser.add_argument("--top1-threshold", type=float, default=0.0)
    parser.add_argument("--margin-threshold", type=float, default=0.2)
    parser.add_argument("--profiles-only", action="store_true")
    parser.add_argument("--require-reranker", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    profiles = build_node_profiles(args.kag_dir, evidence_snippet_chars=args.evidence_snippet_chars)
    profile_count = write_jsonl(args.kag_dir / "kg_node_profiles.jsonl", profiles)
    bm25_manifest = write_profile_bm25(args.kag_dir, profiles)
    manifest: dict[str, Any] = {
        "status": "profiles_ready" if args.profiles_only else "ready",
        "generated_at": now_iso(),
        "kg_node_profiles": {"path": "kg_node_profiles.jsonl", "count": profile_count},
        "bm25": bm25_manifest,
        "domain_alignment_edges": "domain_alignment_edges.jsonl",
        "domain_alignment_review_queue": "domain_alignment_review_queue.jsonl",
        "original_kg_modified": False,
    }
    if args.profiles_only:
        write_json(args.kag_dir / "indexes" / "domain_alignment" / "manifest.json", manifest)
        print(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))
        return
    chunks = list(iter_jsonl(args.kag_dir / "chunks.jsonl"))
    mentions = extract_domain_mentions(chunks, build_alias_map(profiles), max_mentions_per_chunk=args.max_mentions_per_chunk)
    mention_count = write_jsonl(args.kag_dir / "domain_mentions.jsonl", mentions)
    bm25_index = load_bm25_index(args.kag_dir / "indexes" / "domain_alignment" / "node_profiles" / "bm25.json.gz")
    candidates = build_candidates(mentions, profiles, bm25_index, top_k=args.candidate_top_k)
    candidate_count = write_jsonl(args.kag_dir / "domain_alignment_candidates.jsonl", candidates)
    reranker = LocalReranker(args.reranker_model_dir, max_length=args.reranker_max_length, required=args.require_reranker)
    edges, review = finalize_alignments(
        candidates,
        profiles,
        reranker=reranker,
        top1_threshold=args.top1_threshold,
        margin_threshold=args.margin_threshold,
    )
    edge_count = write_jsonl(args.kag_dir / "domain_alignment_edges.jsonl", edges)
    review_count = write_jsonl(args.kag_dir / "domain_alignment_review_queue.jsonl", review)
    manifest.update(
        {
            "mentions": mention_count,
            "candidates": candidate_count,
            "edges": edge_count,
            "review_queue": review_count,
            "reranker_available": reranker.available,
            "source_layers": ["clinical_authority", "external_reference"],
        }
    )
    write_json(args.kag_dir / "indexes" / "domain_alignment" / "manifest.json", manifest)
    print(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
