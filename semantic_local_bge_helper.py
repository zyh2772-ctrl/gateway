#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any


def clamp_score(value: float) -> float:
    return max(0.0, min(1.0, value))


def normalize_hybrid_weights(dense_weight: float, sparse_weight: float) -> tuple[float, float]:
    dense = max(0.0, dense_weight)
    sparse = max(0.0, sparse_weight)
    total = dense + sparse
    if total <= 0.0:
        return 0.7, 0.3
    return dense / total, sparse / total


def load_payload() -> dict[str, Any]:
    raw = sys.stdin.read()
    if not raw.strip():
        raise SystemExit("empty_payload")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise SystemExit("invalid_payload")
    return payload


def main() -> int:
    payload = load_payload()
    model_path = Path(str(payload["model_path"])).expanduser().resolve()
    cache_dir = Path(str(payload["cache_dir"])).expanduser().resolve()
    query = str(payload["query"])
    candidates = payload.get("candidates", [])
    batch_size = max(1, int(payload.get("batch_size", 8)))
    use_fp16 = bool(payload.get("use_fp16", False))
    max_query_length = max(8, int(payload.get("max_query_length", 256)))
    max_passage_length = max(32, int(payload.get("max_passage_length", 512)))
    dense_weight, sparse_weight = normalize_hybrid_weights(
        float(payload.get("dense_weight", 0.7)),
        float(payload.get("sparse_weight", 0.3)),
    )

    cache_dir.mkdir(parents=True, exist_ok=True)
    hub_cache_dir = cache_dir / "hub"
    transformers_cache_dir = cache_dir / "transformers"
    hub_cache_dir.mkdir(parents=True, exist_ok=True)
    transformers_cache_dir.mkdir(parents=True, exist_ok=True)
    os.environ["HF_HOME"] = str(cache_dir)
    os.environ["HUGGINGFACE_HUB_CACHE"] = str(hub_cache_dir)
    os.environ["TRANSFORMERS_CACHE"] = str(transformers_cache_dir)

    from FlagEmbedding import BGEM3FlagModel

    model = BGEM3FlagModel(str(model_path), use_fp16=use_fp16)
    scores: dict[str, float] = {}
    details: dict[str, dict[str, float]] = {}
    embedded_count = 0
    for start in range(0, len(candidates), batch_size):
        batch = candidates[start : start + batch_size]
        sentence_pairs = [(query, str(item["embedding_text"])) for item in batch]
        raw_scores = model.compute_score(
            sentence_pairs,
            batch_size=len(sentence_pairs),
            max_query_length=max_query_length,
            max_passage_length=max_passage_length,
            weights_for_different_modes=[dense_weight, sparse_weight, 0.0],
        )
        dense_values = raw_scores.get("dense", [])
        sparse_values = raw_scores.get("sparse", [])
        hybrid_values = raw_scores.get("sparse+dense", [])
        for index, candidate in enumerate(batch):
            candidate_id = str(candidate["candidate_id"])
            dense_score = clamp_score(float(dense_values[index])) if index < len(dense_values) else 0.0
            sparse_score = clamp_score(float(sparse_values[index])) if index < len(sparse_values) else 0.0
            hybrid_score = clamp_score(float(hybrid_values[index])) if index < len(hybrid_values) else 0.0
            scores[candidate_id] = hybrid_score
            details[candidate_id] = {
                "dense_score": dense_score,
                "sparse_score": sparse_score,
                "hybrid_score": hybrid_score,
            }
            embedded_count += 1

    print(
        json.dumps(
            {
                "ok": True,
                "scores": scores,
                "details": details,
                "embedded_count": embedded_count,
                "dense_weight": dense_weight,
                "sparse_weight": sparse_weight,
                "model_path": str(model_path),
                "cache_dir": str(cache_dir),
                "runner": sys.executable,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
