from __future__ import annotations

import gzip
import json
import re
from collections import Counter, defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import jieba
import numpy as np


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_KAG_DIR = PROJECT_ROOT / "data" / "kb" / "kag_bge_m3"
DEFAULT_EMBEDDING_MODEL_DIR = PROJECT_ROOT / "models" / "embeddings" / "bge-m3"
DEFAULT_RERANKER_MODEL_DIR = PROJECT_ROOT / "models" / "rerankers" / "bge-reranker-v2-m3"
TOKEN_RE = re.compile(r"[A-Za-z0-9]+|[\u4e00-\u9fff]+")


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


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


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


def parse_csv_set(value: str | None) -> set[str] | None:
    if not value:
        return None
    parsed = {item.strip() for item in value.split(",") if item.strip()}
    return parsed or None


@dataclass(frozen=True)
class SolverConfig:
    kag_dir: Path = DEFAULT_KAG_DIR
    embedding_model_dir: Path = DEFAULT_EMBEDDING_MODEL_DIR
    reranker_model_dir: Path = DEFAULT_RERANKER_MODEL_DIR
    enable_dense: bool = True
    rerank_strategy: str = "model"
    require_reranker: bool = True
    dense_max_length: int = 512
    dense_num_threads: int = 4
    reranker_max_length: int = 512
    ppr_alpha: float = 0.18
    ppr_iterations: int = 18
    ppr_top_k: int = 80
    ppr_max_path_hops: int = 3
    quality_gate_suppress_duplicates: bool = True


class BGEEncoder:
    def __init__(self, model_dir: Path, manifest: dict[str, Any], *, max_length: int, num_threads: int) -> None:
        self.model_dir = model_dir
        self.max_length = max_length
        self.num_threads = num_threads
        self.provider = text(manifest.get("provider") or "sentence_transformers")
        self._encode = self._load_encoder()

    def encode(self, values: list[str]) -> np.ndarray:
        return self._encode(values)

    def _load_encoder(self):
        if self.provider == "sentence_transformers":
            try:
                from sentence_transformers import SentenceTransformer

                try:
                    model = SentenceTransformer(str(self.model_dir), local_files_only=True)
                except TypeError:
                    model = SentenceTransformer(str(self.model_dir))
            except Exception:
                if (self.model_dir / "onnx" / "model.onnx").exists():
                    self.provider = "onnxruntime_sentence_transformers_compatible"
                    return self._load_onnx_encoder()
                raise

            def encode(values: list[str]) -> np.ndarray:
                vectors = model.encode(
                    values,
                    batch_size=16,
                    normalize_embeddings=True,
                    show_progress_bar=False,
                )
                return np.asarray(vectors, dtype=np.float32)

            return encode
        if self.provider == "onnxruntime":
            return self._load_onnx_encoder()
        raise RuntimeError(f"Unsupported dense provider in manifest: {self.provider}")

    def _load_onnx_encoder(self):
        onnx_path = self.model_dir / "onnx" / "model.onnx"
        if not onnx_path.exists():
            raise FileNotFoundError(f"BGE-M3 model is missing: {self.model_dir}")
        import onnxruntime as ort
        from transformers import AutoTokenizer

        tokenizer = AutoTokenizer.from_pretrained(str(self.model_dir), local_files_only=True)
        pooling_config_path = self.model_dir / "1_Pooling" / "config.json"
        pooling_config = load_json(pooling_config_path) if pooling_config_path.exists() else {}
        use_cls_pooling = bool(pooling_config.get("pooling_mode_cls_token", False))
        session_options = ort.SessionOptions()
        session_options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        session_options.intra_op_num_threads = max(1, self.num_threads)
        session_options.inter_op_num_threads = 1
        session = ort.InferenceSession(
            str(onnx_path),
            sess_options=session_options,
            providers=["CPUExecutionProvider"],
        )
        input_names = {item.name for item in session.get_inputs()}
        output_names = [item.name for item in session.get_outputs()]

        def encode(values: list[str]) -> np.ndarray:
            encoded = tokenizer(
                values,
                padding=True,
                truncation=True,
                max_length=self.max_length,
                return_tensors="np",
            )
            feeds = {key: value.astype(np.int64) for key, value in encoded.items() if key in input_names}
            outputs = session.run(output_names, feeds)
            token_embeddings = outputs[0]
            if use_cls_pooling:
                pooled = token_embeddings[:, 0, :]
            else:
                mask = encoded["attention_mask"].astype(np.float32)[..., None]
                pooled = (token_embeddings * mask).sum(axis=1) / np.clip(mask.sum(axis=1), 1e-9, None)
            pooled = pooled.astype(np.float32)
            pooled = pooled / np.clip(np.linalg.norm(pooled, axis=1, keepdims=True), 1e-9, None)
            return pooled.astype(np.float32)

        return encode


class BGEReranker:
    def __init__(self, model_dir: Path, *, max_length: int, required: bool) -> None:
        self.available = False
        self.model_dir = model_dir
        self.max_length = max_length
        if not model_dir.exists():
            if required:
                raise FileNotFoundError(f"bge-reranker-v2-m3 model is missing: {model_dir}")
            self._score_pairs = None
            return
        self._score_pairs = self._load_transformers_reranker()
        self.available = True

    def score(self, query: str, docs: list[str]) -> list[float]:
        if not self._score_pairs:
            raise RuntimeError("Reranker is not available; no final score was produced.")
        return self._score_pairs(query, docs)

    def _load_transformers_reranker(self):
        import torch
        from transformers import AutoModelForSequenceClassification, AutoTokenizer

        tokenizer = AutoTokenizer.from_pretrained(str(self.model_dir), local_files_only=True)
        model = AutoModelForSequenceClassification.from_pretrained(str(self.model_dir), local_files_only=True)
        model.eval()
        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        model.to(device)

        def score_pairs(query: str, docs: list[str]) -> list[float]:
            scores: list[float] = []
            batch_size = 8
            with torch.no_grad():
                for start in range(0, len(docs), batch_size):
                    batch_docs = docs[start : start + batch_size]
                    encoded = tokenizer(
                        [query] * len(batch_docs),
                        batch_docs,
                        padding=True,
                        truncation=True,
                        max_length=self.max_length,
                        return_tensors="pt",
                    )
                    encoded = {key: value.to(device) for key, value in encoded.items()}
                    logits = model(**encoded).logits.detach().float().view(-1)
                    scores.extend(float(value) for value in logits.cpu().tolist())
            return scores

        return score_pairs


class KAGSolver:
    def __init__(self, config: SolverConfig | None = None) -> None:
        self.config = config or SolverConfig()
        self.kag_dir = self.config.kag_dir
        self.docs = list(iter_jsonl(self.kag_dir / "retrieval_corpus.jsonl"))
        self.doc_index_by_id = {doc["doc_id"]: index for index, doc in enumerate(self.docs)}
        self.docs_by_id = {doc["doc_id"]: doc for doc in self.docs}
        self.sparse_index = self._load_sparse_index()
        self.alias_to_nodes = self._load_aliases()
        self.adjacency = load_json(self.kag_dir / "indexes" / "graph" / "adjacency.json")
        self.graph_neighbors = self._build_ppr_neighbors()
        self.domain_alignment_by_chunk = self._load_domain_alignments()
        self.encoder: BGEEncoder | None = None
        self.faiss_index = None
        if self.config.enable_dense:
            self.dense_manifest = self._load_dense_manifest()
            self.faiss_index = self._load_faiss_index()
            self.encoder = BGEEncoder(
                self.config.embedding_model_dir,
                self.dense_manifest,
                max_length=self.config.dense_max_length,
                num_threads=self.config.dense_num_threads,
            )
        else:
            self.dense_manifest = {"status": "disabled"}
        self.rerank_strategy = text(self.config.rerank_strategy or "model").lower()
        if self.rerank_strategy not in {"model", "rrf"}:
            raise ValueError(f"Unsupported rerank_strategy: {self.config.rerank_strategy}")
        self.reranker: BGEReranker | None = None
        if self.rerank_strategy == "model":
            self.reranker = BGEReranker(
                self.config.reranker_model_dir,
                max_length=self.config.reranker_max_length,
                required=self.config.require_reranker,
            )
        self._chunks: dict[str, dict[str, Any]] | None = None
        self._nodes: dict[str, dict[str, Any]] | None = None
        self._questions: dict[str, dict[str, Any]] | None = None

    def query(
        self,
        query: str,
        *,
        task_type: str = "concept_qa",
        top_k: int = 8,
        dense_k: int = 40,
        sparse_k: int = 40,
        alias_k: int = 12,
        ppr_k: int | None = None,
        rerank_k: int = 20,
        doc_types: set[str] | None = None,
        source_layers: set[str] | None = None,
    ) -> dict[str, Any]:
        pack = self.retrieve_candidates(
            query,
            dense_k=dense_k,
            sparse_k=sparse_k,
            alias_k=alias_k,
            ppr_k=ppr_k,
            doc_types=doc_types,
            source_layers=source_layers,
        )
        ranked = self.rerank_candidates(query, pack["gated_candidates"][:rerank_k], top_k=top_k)
        hydrated = [self.hydrate_result(item) for item in ranked]
        return self.build_agent_payload(query, task_type=task_type, candidate_pack=pack, results=hydrated)

    def retrieve_candidates(
        self,
        query: str,
        *,
        dense_k: int = 40,
        sparse_k: int = 40,
        alias_k: int = 12,
        ppr_k: int | None = None,
        doc_types: set[str] | None = None,
        source_layers: set[str] | None = None,
    ) -> dict[str, Any]:
        aliases = self.alias_seed_nodes(query, top_k=alias_k)
        sparse = self.bm25_search(query, top_k=sparse_k, doc_types=doc_types, source_layers=source_layers)
        dense = []
        if self.config.enable_dense:
            dense = self.dense_search(query, top_k=dense_k, doc_types=doc_types, source_layers=source_layers)
        seeds = self.collect_graph_seeds(aliases, sparse, dense)
        ppr = self.graph_ppr(seeds, top_k=ppr_k or self.config.ppr_top_k)
        candidates = self.union_candidates(aliases, sparse, dense, ppr)
        gated = self.apply_quality_gate(candidates, doc_types=doc_types, source_layers=source_layers)
        return {
            "aliases": aliases,
            "sparse": sparse,
            "dense": dense,
            "seeds": seeds,
            "ppr": ppr,
            "candidates": candidates,
            "gated_candidates": gated,
        }

    def _load_sparse_index(self) -> dict[str, Any]:
        with gzip.open(self.kag_dir / "indexes" / "sparse" / "inverted_index.json.gz", "rt", encoding="utf-8") as fh:
            return json.load(fh)

    def _load_aliases(self) -> dict[str, list[str]]:
        alias_to_nodes: dict[str, list[str]] = defaultdict(list)
        for row in iter_jsonl(self.kag_dir / "indexes" / "graph" / "aliases.jsonl"):
            alias = text(row.get("alias")).lower()
            node_id = text(row.get("node_id"))
            if alias and node_id and node_id not in alias_to_nodes[alias]:
                alias_to_nodes[alias].append(node_id)
        return dict(alias_to_nodes)

    def _load_dense_manifest(self) -> dict[str, Any]:
        path = self.kag_dir / "indexes" / "dense_bge_m3" / "manifest.json"
        if not path.exists():
            raise FileNotFoundError(f"Dense BGE-M3 manifest is missing: {path}")
        manifest = load_json(path)
        if manifest.get("status") != "ready":
            raise RuntimeError(f"Dense BGE-M3 index is not ready: {manifest.get('status')}")
        return manifest

    def _load_faiss_index(self):
        import faiss

        dense_dir = self.kag_dir / "indexes" / "dense_bge_m3"
        index_path = dense_dir / str((self.dense_manifest.get("faiss") or {}).get("path") or "faiss.index")
        if not index_path.exists():
            raise FileNotFoundError(f"FAISS index is missing: {index_path}")
        return faiss.read_index(str(index_path))

    def _load_rows(self, path: Path, id_field: str) -> dict[str, dict[str, Any]]:
        rows: dict[str, dict[str, Any]] = {}
        for row in iter_jsonl(path):
            row_id = text(row.get(id_field))
            if row_id:
                rows[row_id] = row
        return rows

    @property
    def chunks(self) -> dict[str, dict[str, Any]]:
        if self._chunks is None:
            self._chunks = self._load_rows(self.kag_dir / "chunks.jsonl", "chunk_id")
        return self._chunks

    @property
    def nodes(self) -> dict[str, dict[str, Any]]:
        if self._nodes is None:
            self._nodes = self._load_rows(self.kag_dir / "kg_nodes.jsonl", "node_id")
        return self._nodes

    @property
    def questions(self) -> dict[str, dict[str, Any]]:
        if self._questions is None:
            self._questions = self._load_rows(self.kag_dir / "questions.jsonl", "question_id")
        return self._questions

    def _load_domain_alignments(self) -> dict[str, list[dict[str, Any]]]:
        path = self.kag_dir / "domain_alignment_edges.jsonl"
        alignments: dict[str, list[dict[str, Any]]] = defaultdict(list)
        if not path.exists():
            return {}
        for row in iter_jsonl(path):
            from_id = text(row.get("from_id"))
            to_id = text(row.get("to_id"))
            if from_id and to_id:
                alignments[from_id].append(row)
        return dict(alignments)

    def _build_ppr_neighbors(self) -> dict[str, list[tuple[str, str]]]:
        neighbors: dict[str, list[tuple[str, str]]] = defaultdict(list)
        seen: set[tuple[str, str, str]] = set()

        def add_edge(src: str, tgt: str, relation: str) -> None:
            if not src or not tgt:
                return
            key = (src, tgt, relation)
            if key in seen:
                return
            seen.add(key)
            neighbors[src].append((tgt, relation))

        for from_id, edges in self.adjacency.items():
            for edge in edges:
                to_id = text(edge.get("to_id"))
                relation = text(edge.get("relation") or "related")
                add_edge(from_id, to_id, relation)
                if edge.get("direction") in {"evidence", "assessment"}:
                    add_edge(to_id, from_id, f"reverse:{relation}")
        domain_path = self.kag_dir / "domain_alignment_edges.jsonl"
        if domain_path.exists():
            for edge in iter_jsonl(domain_path):
                from_id = text(edge.get("from_id"))
                to_id = text(edge.get("to_id"))
                relation = text(edge.get("relation") or "domain_alignment")
                add_edge(from_id, to_id, relation)
                add_edge(to_id, from_id, f"reverse:{relation}")
        return dict(neighbors)

    def alias_seed_nodes(self, query: str, *, top_k: int) -> list[dict[str, Any]]:
        query_lc = query.lower()
        seeds: list[dict[str, Any]] = []
        seen: set[str] = set()
        for alias, node_ids in self.alias_to_nodes.items():
            if alias and (alias in query_lc or query_lc in alias):
                score = len(alias) / max(len(query_lc), len(alias), 1)
                for node_id in node_ids:
                    if node_id in seen:
                        continue
                    seen.add(node_id)
                    seeds.append(
                        {
                            "doc_id": node_id,
                            "doc_type": "kg_node",
                            "source_layer": "textbook_kg",
                            "retrieval_score": round(2.0 + score, 6),
                            "matched_by": ["alias"],
                            "matched_alias": alias,
                        }
                    )
        return sorted(seeds, key=lambda item: item["retrieval_score"], reverse=True)[:top_k]

    def bm25_search(
        self,
        query: str,
        *,
        top_k: int,
        doc_types: set[str] | None = None,
        source_layers: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        query_terms = Counter(tokenize(query))
        if not query_terms:
            return []
        doc_terms = self.sparse_index["doc_terms"]
        avg_len = float(self.sparse_index.get("avg_doc_len") or 1.0)
        idf = self.sparse_index["idf"]
        postings = self.sparse_index["postings"]
        k1 = float(self.sparse_index.get("k1") or 1.5)
        b = float(self.sparse_index.get("b") or 0.75)
        scores: dict[int, float] = defaultdict(float)
        for term, qtf in query_terms.items():
            term_idf = float(idf.get(term) or 0.0)
            if term_idf <= 0:
                continue
            for doc_index, tf in postings.get(term, []):
                doc = self.docs[doc_index]
                if not self._doc_allowed(doc, doc_types=doc_types, source_layers=source_layers):
                    continue
                doc_len = max(float(doc_terms[doc_index].get("length") or 1.0), 1.0)
                denom = tf + k1 * (1 - b + b * doc_len / avg_len)
                scores[doc_index] += qtf * term_idf * (tf * (k1 + 1)) / denom
        ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)[:top_k]
        return [
            {
                "doc_id": self.docs[index]["doc_id"],
                "doc_type": self.docs[index].get("doc_type"),
                "source_layer": self.docs[index].get("source_layer"),
                "retrieval_score": round(score, 6),
                "matched_by": ["bm25"],
                "rank": rank,
            }
            for rank, (index, score) in enumerate(ranked, 1)
        ]

    def dense_search(
        self,
        query: str,
        *,
        top_k: int,
        doc_types: set[str] | None = None,
        source_layers: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        if self.encoder is None or self.faiss_index is None:
            return []
        query_vector = self.encoder.encode([query])
        search_k = min(max(top_k * 4, top_k), len(self.docs))
        scores, indices = self.faiss_index.search(query_vector, search_k)
        results: list[dict[str, Any]] = []
        for score, doc_index in zip(scores[0], indices[0]):
            if doc_index < 0:
                continue
            doc = self.docs[int(doc_index)]
            if not self._doc_allowed(doc, doc_types=doc_types, source_layers=source_layers):
                continue
            results.append(
                {
                    "doc_id": doc["doc_id"],
                    "doc_type": doc.get("doc_type"),
                    "source_layer": doc.get("source_layer"),
                    "retrieval_score": round(float(score), 6),
                    "matched_by": ["dense_bge_m3"],
                    "rank": len(results) + 1,
                }
            )
            if len(results) >= top_k:
                break
        return results

    def collect_graph_seeds(self, *groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
        seen: set[str] = set()
        seeds: list[dict[str, Any]] = []
        for group in groups:
            for item in group:
                doc_id = text(item.get("doc_id"))
                doc = self.docs_by_id.get(doc_id)
                if not doc:
                    continue
                node_ids: list[str] = []
                if doc.get("doc_type") == "kg_node":
                    node_ids.append(doc_id)
                else:
                    metadata = doc.get("metadata") if isinstance(doc.get("metadata"), dict) else {}
                    node_ids.extend(text(node_id) for node_id in as_list(metadata.get("core_node_ids")) if text(node_id))
                    primary_node_id = text(metadata.get("primary_node_id"))
                    if primary_node_id:
                        node_ids.append(primary_node_id)
                for node_id in node_ids:
                    if node_id in seen or node_id not in self.docs_by_id:
                        continue
                    seen.add(node_id)
                    seeds.append(
                        {
                            "doc_id": node_id,
                            "matched_by": as_list(item.get("matched_by")),
                            "seed_score": item.get("retrieval_score"),
                            "seed_source_doc_id": doc_id,
                        }
                    )
        return seeds

    def graph_ppr(self, seed_items: list[dict[str, Any]], *, top_k: int) -> list[dict[str, Any]]:
        seed_ids = list(dict.fromkeys(text(item.get("doc_id")) for item in seed_items if text(item.get("doc_id"))))
        if not seed_ids:
            return []
        seed = {node_id: 1.0 / len(seed_ids) for node_id in seed_ids}
        scores = dict(seed)
        alpha = self.config.ppr_alpha
        for _ in range(max(1, self.config.ppr_iterations)):
            next_scores: dict[str, float] = defaultdict(float)
            for node_id, value in scores.items():
                neighbors = self.graph_neighbors.get(node_id) or []
                if not neighbors:
                    next_scores[node_id] += (1.0 - alpha) * value
                    continue
                share = (1.0 - alpha) * value / len(neighbors)
                for target_id, _ in neighbors:
                    next_scores[target_id] += share
            for node_id, value in seed.items():
                next_scores[node_id] += alpha * value
            scores = dict(next_scores)
        results: list[dict[str, Any]] = []
        for doc_id, score in sorted(scores.items(), key=lambda item: item[1], reverse=True):
            doc = self.docs_by_id.get(doc_id)
            if not doc:
                continue
            results.append(
                {
                    "doc_id": doc_id,
                    "doc_type": doc.get("doc_type"),
                    "source_layer": doc.get("source_layer"),
                    "graph_score": round(float(score), 8),
                    "matched_by": ["graph_ppr"],
                }
            )
            if len(results) >= top_k:
                break
        return results

    def union_candidates(self, *groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
        merged: dict[str, dict[str, Any]] = {}
        order = 0
        for group in groups:
            for item in group:
                doc_id = text(item.get("doc_id"))
                if not doc_id or doc_id not in self.docs_by_id:
                    continue
                existing = merged.get(doc_id)
                if not existing:
                    order += 1
                    existing = {
                        "doc_id": doc_id,
                        "doc_type": item.get("doc_type"),
                        "source_layer": item.get("source_layer"),
                        "matched_by": [],
                        "retrieval_scores": {},
                        "graph_score": None,
                        "_order": order,
                    }
                    merged[doc_id] = existing
                for stage in as_list(item.get("matched_by")):
                    if stage and stage not in existing["matched_by"]:
                        existing["matched_by"].append(stage)
                    if stage and item.get("retrieval_score") is not None:
                        existing["retrieval_scores"][stage] = item.get("retrieval_score")
                if item.get("graph_score") is not None:
                    existing["graph_score"] = item.get("graph_score")
                if "matched_alias" in item and "matched_alias" not in existing:
                    existing["matched_alias"] = item["matched_alias"]
        return sorted(
            merged.values(),
            key=lambda item: (len(item.get("matched_by") or []), float(item.get("graph_score") or 0.0), -int(item.get("_order") or 0)),
            reverse=True,
        )

    def apply_quality_gate(
        self,
        candidates: list[dict[str, Any]],
        *,
        doc_types: set[str] | None,
        source_layers: set[str] | None,
    ) -> list[dict[str, Any]]:
        gated: list[dict[str, Any]] = []
        for item in candidates:
            doc = self.docs_by_id.get(item["doc_id"])
            if not doc or not self._doc_allowed(doc, doc_types=doc_types, source_layers=source_layers):
                continue
            if not text(doc.get("text")):
                continue
            metadata = doc.get("metadata") if isinstance(doc.get("metadata"), dict) else {}
            if self.config.quality_gate_suppress_duplicates and metadata.get("suppress_duplicate"):
                continue
            flags = set(text(flag) for flag in as_list(metadata.get("quality_flags")) if text(flag))
            if "boilerplate_disclaimer" in flags and doc.get("source_layer") == "external_reference":
                continue
            gated.append(item)
        return gated

    def rerank_candidates(self, query: str, candidates: list[dict[str, Any]], *, top_k: int) -> list[dict[str, Any]]:
        if not candidates:
            return []
        if self.rerank_strategy == "rrf":
            return self.rrf_rank_candidates(candidates, top_k=top_k)
        if self.reranker is None or not self.reranker.available:
            if self.config.require_reranker:
                raise RuntimeError("Reranker is required but not available.")
            return self.rrf_rank_candidates(candidates, top_k=top_k)
        docs = [text(self.docs_by_id[item["doc_id"]].get("text"))[:4000] for item in candidates]
        scores = self.reranker.score(query, docs)
        ranked = [{**item, "rerank_score": round(float(score), 6)} for item, score in zip(candidates, scores)]
        return sorted(ranked, key=lambda item: item.get("rerank_score") or 0.0, reverse=True)[:top_k]

    def rrf_rank_candidates(
        self,
        candidates: list[dict[str, Any]],
        *,
        top_k: int,
        rrf_k: int = 60,
    ) -> list[dict[str, Any]]:
        stage_scores: dict[str, dict[str, float]] = defaultdict(dict)
        for item in candidates:
            doc_id = text(item.get("doc_id"))
            if not doc_id:
                continue
            retrieval_scores = item.get("retrieval_scores")
            if isinstance(retrieval_scores, dict):
                for stage, score in retrieval_scores.items():
                    try:
                        stage_scores[text(stage)][doc_id] = float(score)
                    except (TypeError, ValueError):
                        continue
            graph_score = item.get("graph_score")
            if graph_score is not None:
                try:
                    stage_scores["graph_ppr"][doc_id] = float(graph_score)
                except (TypeError, ValueError):
                    pass

        rrf_scores: dict[str, float] = defaultdict(float)
        rank_features: dict[str, dict[str, int]] = defaultdict(dict)
        for stage, scores in stage_scores.items():
            ranked_doc_ids = [
                doc_id
                for doc_id, _ in sorted(scores.items(), key=lambda pair: pair[1], reverse=True)
            ]
            for rank, doc_id in enumerate(ranked_doc_ids, 1):
                rrf_scores[doc_id] += 1.0 / (rrf_k + rank)
                rank_features[doc_id][stage] = rank

        ranked: list[dict[str, Any]] = []
        for item in candidates:
            doc_id = text(item.get("doc_id"))
            score = rrf_scores.get(doc_id, 0.0)
            if score <= 0:
                score = 1.0 / (rrf_k + int(item.get("_order") or 9999))
            ranked.append(
                {
                    **item,
                    "fusion_score": round(float(score), 8),
                    "ranker": "rrf",
                    "rank_features": dict(rank_features.get(doc_id, {})),
                }
            )
        return sorted(
            ranked,
            key=lambda item: (
                float(item.get("fusion_score") or 0.0),
                len(item.get("matched_by") or []),
                float(item.get("graph_score") or 0.0),
                -int(item.get("_order") or 0),
            ),
            reverse=True,
        )[:top_k]

    def hydrate_result(self, result: dict[str, Any]) -> dict[str, Any]:
        doc_id = result["doc_id"]
        row = self.chunks.get(doc_id) or self.nodes.get(doc_id) or self.questions.get(doc_id) or self.docs_by_id.get(doc_id) or {}
        doc = self.docs_by_id.get(doc_id) or {}
        metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        doc_metadata = doc.get("metadata") if isinstance(doc.get("metadata"), dict) else {}
        preview = text(row.get("text") or row.get("stem") or row.get("canonical_name") or doc.get("text"))
        return {
            **{key: value for key, value in result.items() if not key.startswith("_")},
            "title": text(row.get("canonical_name") or row.get("source_title") or row.get("stem") or doc_id)[:160],
            "source_layer": result.get("source_layer") or row.get("source_layer") or doc.get("source_layer"),
            "source_type": row.get("source_type") or metadata.get("source_type"),
            "subject": row.get("subject") or metadata.get("subject") or doc_metadata.get("subject"),
            "chapter": row.get("chapter") or metadata.get("chapter") or doc_metadata.get("chapter"),
            "heading_path": row.get("heading_path") or metadata.get("heading_path") or [],
            "chunk_role": row.get("chunk_role") or metadata.get("chunk_role"),
            "source_span_ids": as_list(row.get("source_span_ids") or metadata.get("source_span_ids") or doc_metadata.get("source_span_ids")),
            "core_node_ids": as_list(row.get("core_node_ids") or metadata.get("core_node_ids") or doc_metadata.get("core_node_ids")),
            "quality_flags": row.get("quality_flags") or metadata.get("quality_flags") or doc_metadata.get("quality_flags") or [],
            "preview": preview[:500],
            "domain_alignments": self.domain_alignment_by_chunk.get(doc_id, []),
        }

    def build_agent_payload(self, query: str, *, task_type: str, candidate_pack: dict[str, Any], results: list[dict[str, Any]]) -> dict[str, Any]:
        evidence_chunks = [item for item in results if item.get("doc_type") == "chunk"]
        kg_nodes = [self.node_payload(item) for item in results if item.get("doc_type") == "kg_node"]
        kg_paths = self.build_paths([seed["doc_id"] for seed in candidate_pack["seeds"]], results)
        citations = self.build_citations(evidence_chunks)
        operators = ["alias", "bm25"]
        operators.append("dense_bge_m3" if self.config.enable_dense else "dense_disabled")
        operators.append("graph_ppr")
        operators.append("bge_reranker_v2_m3" if self.rerank_strategy == "model" else "rrf_fusion")
        return {
            "query": query,
            "task_type": task_type,
            "retrieval_trace": {
                "operators": operators,
                "dense_enabled": self.config.enable_dense,
                "dense_provider": getattr(self.encoder, "provider", None),
                "ranker": self.rerank_strategy,
                "candidate_count": len(candidate_pack["candidates"]),
                "gated_candidate_count": len(candidate_pack["gated_candidates"]),
                "returned_count": len(results),
                "alias_candidates": len(candidate_pack["aliases"]),
                "sparse_candidates": len(candidate_pack["sparse"]),
                "dense_candidates": len(candidate_pack["dense"]),
                "graph_candidates": len(candidate_pack["ppr"]),
                "graph_seed_count": len(candidate_pack["seeds"]),
            },
            "grounded_context": {
                "answer_mode": "evidence_bound",
                "generation_policy": {
                    "must_cite_every_medical_claim": True,
                    "allow_abstraction": "limited",
                    "allow_add_external_knowledge": False,
                    "unsupported_behavior": "omit_or_state_insufficient_evidence",
                    "critical_terms_copy_mode": True,
                },
                "atomic_claims": [],
                "claim_extraction_status": "not_generated_by_retrieval_solver",
                "evidence_units": [
                    {
                        "evidence_id": f"ev_{index:03d}",
                        "chunk_id": item["doc_id"],
                        "text": item.get("preview", ""),
                        "source_title": item.get("title"),
                        "page_start": self.chunks.get(item["doc_id"], {}).get("page_start"),
                        "page_end": self.chunks.get(item["doc_id"], {}).get("page_end"),
                        "source_span_ids": item.get("source_span_ids") or [],
                        "source_layer": item.get("source_layer"),
                    }
                    for index, item in enumerate(evidence_chunks, 1)
                ],
                "do_not_infer": [
                    "Do not add dosage, contraindication, diagnostic threshold, or treatment recommendation absent from evidence.",
                    "Do not convert external references into textbook or clinical-consensus claims.",
                    "State insufficient evidence when retrieved evidence does not support a medical claim.",
                ],
            },
            "evidence_chunks": evidence_chunks,
            "kg_nodes": kg_nodes,
            "kg_paths": kg_paths,
            "citations": citations,
            "domain_alignments": [alignment for item in evidence_chunks for alignment in as_list(item.get("domain_alignments"))],
            "warnings": [],
        }

    def node_payload(self, result: dict[str, Any]) -> dict[str, Any]:
        node = self.nodes.get(result["doc_id"], {})
        return {
            "node_id": result["doc_id"],
            "canonical_name": node.get("canonical_name") or result.get("title"),
            "aliases": node.get("aliases") or [],
            "node_type": node.get("node_type"),
            "subject": node.get("subject"),
            "chapter": node.get("chapter"),
            "matched_by": result.get("matched_by") or [],
            "rerank_score": result.get("rerank_score"),
            "fusion_score": result.get("fusion_score"),
            "ranker": result.get("ranker"),
            "graph_score": result.get("graph_score"),
        }

    def build_citations(self, evidence_chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        citations: list[dict[str, Any]] = []
        for index, item in enumerate(evidence_chunks, 1):
            chunk = self.chunks.get(item["doc_id"], {})
            citations.append(
                {
                    "citation_id": f"cite_{index:03d}",
                    "chunk_id": item["doc_id"],
                    "source_title": item.get("title"),
                    "page_start": chunk.get("page_start"),
                    "page_end": chunk.get("page_end"),
                    "source_span_ids": item.get("source_span_ids") or [],
                    "evidence_text": item.get("preview") or "",
                }
            )
        return citations

    def build_paths(self, seed_ids: list[str], results: list[dict[str, Any]]) -> list[dict[str, Any]]:
        paths: list[dict[str, Any]] = []
        for result in results:
            path = self.find_shortest_path(seed_ids, text(result.get("doc_id")), max_hops=self.config.ppr_max_path_hops)
            if not path:
                continue
            paths.append(
                {
                    "path_id": f"path_{len(paths) + 1:03d}",
                    "nodes": [{"id": node_id, "label": self.label_for_id(node_id)} for node_id, _ in path],
                    "edges": [{"relation": relation} for _, relation in path[1:]],
                    "path_score": result.get("graph_score"),
                }
            )
        return paths

    def find_shortest_path(self, seed_ids: list[str], target_id: str, *, max_hops: int) -> list[tuple[str, str]] | None:
        seed_ids = [seed_id for seed_id in dict.fromkeys(seed_ids) if seed_id]
        if not target_id:
            return None
        if target_id in seed_ids:
            return [(target_id, "self")]
        queue: deque[tuple[str, list[tuple[str, str]], int]] = deque()
        visited: set[str] = set()
        for seed_id in seed_ids:
            queue.append((seed_id, [(seed_id, "seed")], 0))
            visited.add(seed_id)
        while queue:
            current, path, depth = queue.popleft()
            if depth >= max_hops:
                continue
            for neighbor, relation in self.graph_neighbors.get(current, []):
                if neighbor in visited:
                    continue
                next_path = [*path, (neighbor, relation)]
                if neighbor == target_id:
                    return next_path
                visited.add(neighbor)
                queue.append((neighbor, next_path, depth + 1))
        return None

    def label_for_id(self, item_id: str) -> str:
        if item_id in self.nodes:
            return text(self.nodes[item_id].get("canonical_name") or item_id)
        if item_id in self.chunks:
            return text(self.chunks[item_id].get("source_title") or item_id)[:80]
        if item_id in self.questions:
            return text(self.questions[item_id].get("stem") or item_id)[:80]
        return item_id

    def _doc_allowed(self, doc: dict[str, Any], *, doc_types: set[str] | None, source_layers: set[str] | None) -> bool:
        if doc_types and doc.get("doc_type") not in doc_types:
            return False
        if source_layers and doc.get("source_layer") not in source_layers:
            return False
        return True
