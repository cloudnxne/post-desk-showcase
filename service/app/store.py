"""Where each client's page index lives: one JSON file per client, in Cloud Storage (or a local folder for dev).

The file holds every sitemap page that was crawled (title, H1, meta description, a short summary, a hash of
its text, when it was crawled, or why it was skipped) plus the page vectors as one base64 block of float32s.
A few thousand pages is a few MB, small enough to load and search in memory with numpy. At a much larger
scale this would move to a vector database (Vertex AI Vector Search, BigQuery or pgvector).

Settings:
  POST_DESK_BUCKET     Cloud Storage bucket name (production)
  POST_DESK_INDEX_DIR  local folder instead (development)
"""
from __future__ import annotations

import base64
import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import numpy as np

FORMAT_VERSION = 1


@dataclass
class Index:
    domain: str
    model: str
    dims: int
    pages: dict[str, dict] = field(default_factory=dict)     # url -> page record
    vectors: dict[str, np.ndarray] = field(default_factory=dict)  # url -> unit vector (indexed pages only)
    updated_at: float = 0.0

    def to_json(self) -> str:
        urls = [u for u in self.pages if u in self.vectors]
        mat = np.stack([self.vectors[u] for u in urls]).astype(np.float32) if urls else np.zeros((0, self.dims), np.float32)
        return json.dumps({
            "version": FORMAT_VERSION, "domain": self.domain, "model": self.model, "dims": self.dims,
            "updated_at": self.updated_at, "pages": self.pages,
            "vector_urls": urls, "vectors": base64.b64encode(mat.tobytes()).decode(),
        })

    @classmethod
    def from_json(cls, text: str) -> "Index":
        d = json.loads(text)
        if d.get("version") != FORMAT_VERSION:
            raise ValueError(f"Unknown index format {d.get('version')}")
        mat = np.frombuffer(base64.b64decode(d["vectors"]), dtype=np.float32).reshape(-1, d["dims"]) if d["vector_urls"] else []
        return cls(domain=d["domain"], model=d["model"], dims=d["dims"], pages=d["pages"],
                   vectors={u: np.array(mat[i]) for i, u in enumerate(d["vector_urls"])}, updated_at=d.get("updated_at", 0))

    def matrix(self, urls: list[str]) -> np.ndarray:
        return np.stack([self.vectors[u] for u in urls]) if urls else np.zeros((0, self.dims), np.float32)


def _key(domain: str) -> str:
    return "indexes/" + re.sub(r"[^a-z0-9.-]", "_", domain.lower()) + ".json"


class IndexStore(Protocol):
    def load(self, domain: str) -> Index | None: ...
    def save(self, index: Index) -> None: ...
    def domains(self) -> list[str]: ...


def _domain_from_key(name: str) -> str:
    return name.rsplit("/", 1)[-1].removesuffix(".json")


class LocalStore:
    def __init__(self, folder: str | os.PathLike):
        self.folder = Path(folder)

    def load(self, domain):
        p = self.folder / _key(domain)
        return Index.from_json(p.read_text()) if p.exists() else None

    def save(self, index):
        p = self.folder / _key(index.domain)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(index.to_json())

    def domains(self):
        return sorted(_domain_from_key(str(p)) for p in (self.folder / "indexes").glob("*.json"))

    def put_text(self, key: str, text: str):
        p = self.folder / key
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)

    def get_text(self, key: str) -> str | None:
        p = self.folder / key
        return p.read_text() if p.exists() else None


class GCSStore:
    """Connects to Cloud Storage on first use, so a setup problem shows up as a failed index call
    (and a fallback to URL ranking for posts), never as a service that won't answer."""

    def __init__(self, bucket: str, client=None):
        self.bucket_name = bucket
        self._client = client
        self._bucket_obj = None

    @property
    def _bucket(self):
        if self._bucket_obj is None:
            if self._client is None:
                from google.cloud import storage

                self._client = storage.Client()
            self._bucket_obj = self._client.bucket(self.bucket_name)
        return self._bucket_obj

    def load(self, domain):
        blob = self._bucket.blob(_key(domain))
        if not blob.exists():
            return None
        return Index.from_json(blob.download_as_text())

    def save(self, index):
        self._bucket.blob(_key(index.domain)).upload_from_string(index.to_json(), content_type="application/json")

    def domains(self):
        return sorted(_domain_from_key(b.name) for b in self._bucket.list_blobs(prefix="indexes/") if b.name.endswith(".json"))

    def put_text(self, key: str, text: str):
        self._bucket.blob(key).upload_from_string(text, content_type="application/json")

    def get_text(self, key: str) -> str | None:
        blob = self._bucket.blob(key)
        return blob.download_as_text() if blob.exists() else None


class CachedStore:
    """Keeps recently used indexes in memory for a few minutes, so each post doesn't re-download its client's index."""

    def __init__(self, inner: IndexStore, ttl: float = 300):
        self.inner, self.ttl = inner, ttl
        self._cache: dict[str, tuple[float, Index | None]] = {}
        self._lock = threading.Lock()

    def load(self, domain):
        with self._lock:
            hit = self._cache.get(domain)
            if hit and time.monotonic() - hit[0] < self.ttl:
                return hit[1]
        idx = self.inner.load(domain)
        with self._lock:
            self._cache[domain] = (time.monotonic(), idx)
        return idx

    def save(self, index):
        self.inner.save(index)
        with self._lock:
            self._cache[index.domain] = (time.monotonic(), index)

    def domains(self):
        return self.inner.domains()

    def put_text(self, key, text):
        self.inner.put_text(key, text)

    def get_text(self, key):
        return self.inner.get_text(key)


def bucket_name(raw: str) -> str:
    """Tidies the bucket setting: 'gs://my-bucket/ ' -> 'my-bucket'. Raises ValueError, naming the value, if it's still invalid."""
    name = re.sub(r"^gs://", "", (raw or "").strip()).strip("/").strip()
    if name and not re.fullmatch(r"[a-z0-9][a-z0-9._-]{1,220}[a-z0-9]", name):
        raise ValueError(f"POST_DESK_BUCKET is {raw!r}, which isn't a valid bucket name")
    return name


def default_store() -> IndexStore | None:
    if os.environ.get("POST_DESK_BUCKET", "").strip():
        return CachedStore(GCSStore(bucket_name(os.environ["POST_DESK_BUCKET"])))
    if os.environ.get("POST_DESK_INDEX_DIR"):
        return CachedStore(LocalStore(os.environ["POST_DESK_INDEX_DIR"]))
    return None
