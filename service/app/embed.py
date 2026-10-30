"""Text embeddings. Pages are stored as RETRIEVAL_DOCUMENT vectors and posts are matched as RETRIEVAL_QUERY,
which is how retrieval-tuned embedding models expect to be used.

Settings:
  POST_DESK_EMBED_MODEL      default gemini-embedding-001
  POST_DESK_EMBED_DIMS       default 768 (smaller index, near-identical quality for this job)
  POST_DESK_VERTEX_LOCATION  default europe-west2
  GOOGLE_CLOUD_PROJECT       the project to bill
"""
from __future__ import annotations

import hashlib
import os
import random
import re
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Protocol

import numpy as np

DOC, QUERY = "RETRIEVAL_DOCUMENT", "RETRIEVAL_QUERY"


class EmbedError(Exception):
    pass


class Embedder(Protocol):
    name: str
    dims: int

    def embed(self, texts: list[str], task: str) -> np.ndarray: ...


def normalise(m: np.ndarray) -> np.ndarray:
    """Unit-length rows, so a dot product is cosine similarity. Needed when vectors are cut to fewer dimensions."""
    m = np.asarray(m, dtype=np.float32)
    n = np.linalg.norm(m, axis=1, keepdims=True)
    n[n == 0] = 1
    return m / n


class VertexEmbedder:
    def __init__(self, model: str | None = None, dims: int | None = None, project: str | None = None,
                 location: str | None = None, workers: int = 8, client=None):
        self.name = model or os.environ.get("POST_DESK_EMBED_MODEL", "gemini-embedding-001")
        self.dims = int(dims or os.environ.get("POST_DESK_EMBED_DIMS", 768))
        self.workers = workers
        if client is None:
            from google import genai

            project = project or os.environ.get("GOOGLE_CLOUD_PROJECT")
            location = location or os.environ.get("POST_DESK_VERTEX_LOCATION", "europe-west2")
            if not project:
                raise EmbedError("GOOGLE_CLOUD_PROJECT isn't set, so Vertex AI can't be used.")
            from google.genai import types as _t

            from .llm import connect_capped_httpx_client
            client = genai.Client(vertexai=True, project=project, location=location,
                                  http_options=_t.HttpOptions(httpx_client=connect_capped_httpx_client()))
        self._client = client

    def _one(self, text: str, task: str) -> list[float]:
        from google.genai import errors, types

        config = types.EmbedContentConfig(task_type=task, output_dimensionality=self.dims)
        for attempt in range(5):
            try:
                r = self._client.models.embed_content(model=self.name, contents=text, config=config)
                return list(r.embeddings[0].values)
            except errors.APIError as e:
                if getattr(e, "code", None) in (429, 500, 503) and attempt < 4:
                    time.sleep(min(20, 2 ** attempt) + random.random())   # back off and retry when busy or rate limited
                    continue
                raise EmbedError(f"Vertex AI embedding failed ({getattr(e, 'code', '?')}): {getattr(e, 'message', e)}") from e
        raise EmbedError("Vertex AI embedding kept failing.")

    def embed(self, texts: list[str], task: str) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dims), dtype=np.float32)
        # The newer Vertex embedding models take one text per call, so send several at once in parallel.
        with ThreadPoolExecutor(self.workers) as pool:
            vectors = list(pool.map(lambda t: self._one(t, task), texts))
        return normalise(np.array(vectors))


class HashEmbedder:
    """Deterministic stand-in for tests and local runs: shared words give similar vectors, no network.
    Not used in production."""

    name = "hash-embedder"

    def __init__(self, dims: int = 256):
        self.dims = dims
        self.calls = 0
        self.texts: list[str] = []

    def embed(self, texts: list[str], task: str) -> np.ndarray:
        self.calls += 1
        self.texts += texts
        m = np.zeros((len(texts), self.dims), dtype=np.float32)
        for i, t in enumerate(texts):
            for w in re.findall(r"[a-z0-9]+", t.lower()):
                if len(w) > 2:
                    m[i, int(hashlib.md5(w.encode()).hexdigest(), 16) % self.dims] += 1
        return normalise(m)
