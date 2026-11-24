"""Name embeddings for entity resolution: a candidate source and one scoring feature.

Off by default (`embeddings.enabled` in config/matching.yaml, or `poi match --embeddings`).
Local model through fastembed (ONNX, no API calls): install with `uv sync --extra embeddings`.
Embeddings never decide a merge on their own; they add candidate pairs, which the log-odds
model scores like any other, and a binned contribution on cosine similarity.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any, Protocol

import numpy as np
import numpy.typing as npt

from poi.match.scoring import MatchRecord
from poi.settings import PROJECT_ROOT

Vectors = dict[int, npt.NDArray[np.float32]]
CACHE_DIR = PROJECT_ROOT / "data" / "cache" / "fastembed"  # model files, gitignored


class Embedder(Protocol):
    model_name: str

    def embed(self, texts: Sequence[str]) -> npt.NDArray[np.float32]: ...


class FastEmbedder:
    """fastembed's ONNX text model; downloads the model once into data/cache/fastembed."""

    def __init__(self, model_name: str) -> None:
        try:
            from fastembed import TextEmbedding
        except ImportError as exc:  # pragma: no cover - depends on the optional extra
            raise RuntimeError(
                "embeddings need the optional extra: uv sync --extra embeddings"
            ) from exc
        self.model_name = model_name
        self._model = TextEmbedding(model_name=model_name, cache_dir=str(CACHE_DIR))

    def embed(self, texts: Sequence[str]) -> npt.NDArray[np.float32]:
        return np.asarray(list(self._model.embed(list(texts))), dtype=np.float32)


def record_text(record: MatchRecord) -> str | None:
    """The name as written: casing and generic words ("Campground", "Horse Camp") are part
    of what the model compares."""
    if not record.name:
        return None
    return record.name


def vectors(records: Iterable[MatchRecord], embedder: Embedder) -> Vectors:
    """Unit-length vector per record with a name; records without one get none."""
    texts = {r.id: t for r in records if (t := record_text(r))}
    if not texts:
        return {}
    matrix = embedder.embed(list(texts.values()))
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    matrix = (matrix / np.where(norms == 0, 1, norms)).astype(np.float32)
    return dict(zip(texts, matrix, strict=True))


def similarity(vecs: Vectors, a: int, b: int) -> float | None:
    if a not in vecs or b not in vecs:
        return None
    return round(float(vecs[a] @ vecs[b]), 4)


def neighbour_pairs(
    vecs: Vectors, top_k: int, min_similarity: float
) -> dict[tuple[int, int], float]:
    """Each record's `top_k` most similar names at or above `min_similarity`, as (low, high)
    id pairs. Exact: one matrix product, fine for a region (thousands of records); at 1M
    records this becomes an ANN index (see DESIGN.md)."""
    ids = list(vecs)
    if len(ids) < 2:
        return {}
    matrix = np.stack([vecs[i] for i in ids])
    sims = matrix @ matrix.T
    np.fill_diagonal(sims, -1.0)
    k = min(top_k, len(ids) - 1)
    pairs: dict[tuple[int, int], float] = {}
    for row, neighbours in enumerate(np.argsort(-sims, axis=1)[:, :k]):
        for col in neighbours:
            sim = float(sims[row, col])
            if sim < min_similarity:
                break
            a, b = sorted((ids[row], ids[int(col)]))
            pairs[(a, b)] = round(sim, 4)
    return pairs


def settings_summary(cfg: dict[str, Any], embedder: Embedder) -> dict[str, Any]:
    return {
        "model": embedder.model_name,
        "top_k": cfg["top_k"],
        "min_similarity": cfg["min_similarity"],
        "max_distance_m": cfg["max_distance_m"],
        "feature": cfg["use_as_feature"],
    }
