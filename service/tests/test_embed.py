"""The Vertex AI embedder, with a stand-in for Google's client (no network). Checks it asks for the right
task type and size, sends texts one at a time in parallel, retries when rate limited, and explains failures."""
from types import SimpleNamespace

import numpy as np
import pytest
from google.genai import errors

from app import embed
from app.embed import DOC, QUERY, EmbedError, VertexEmbedder


class FakeGoogle:
    def __init__(self, fail_first=0, fail_code=429):
        self.calls, self.fail_first, self.fail_code = [], fail_first, fail_code
        self.models = self

    def embed_content(self, model, contents, config):
        self.calls.append((model, contents, config.task_type, config.output_dimensionality))
        if self.fail_first:
            self.fail_first -= 1
            raise errors.APIError(self.fail_code, {"error": {"message": "busy", "status": "X"}})
        v = [float(len(contents)), 1.0, 0.0, 2.0]
        return SimpleNamespace(embeddings=[SimpleNamespace(values=v)])


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(embed.time, "sleep", lambda s: None)


def test_sends_each_text_with_the_task_type_and_size():
    g = FakeGoogle()
    e = VertexEmbedder(model="gemini-embedding-001", dims=4, client=g)
    m = e.embed(["one", "three"], DOC)
    assert m.shape == (2, 4) and np.allclose(np.linalg.norm(m, axis=1), 1)
    assert sorted(c[1] for c in g.calls) == ["one", "three"]
    assert {(c[0], c[2], c[3]) for c in g.calls} == {("gemini-embedding-001", DOC, 4)}


def test_query_task_type_for_posts():
    g = FakeGoogle()
    VertexEmbedder(dims=4, client=g).embed(["post chunk"], QUERY)
    assert g.calls[0][2] == QUERY


def test_order_is_kept():
    m = VertexEmbedder(dims=4, client=FakeGoogle()).embed(["a", "bbbbbbbbbb"], DOC)
    assert m[0][0] < m[1][0]


def test_retries_when_rate_limited():
    g = FakeGoogle(fail_first=2, fail_code=429)
    VertexEmbedder(dims=4, client=g, workers=1).embed(["x"], DOC)
    assert len(g.calls) == 3


def test_permission_errors_fail_straight_away_with_the_reason():
    g = FakeGoogle(fail_first=1, fail_code=403)
    with pytest.raises(EmbedError, match="403"):
        VertexEmbedder(dims=4, client=g, workers=1).embed(["x"], DOC)
    assert len(g.calls) == 1


def test_needs_a_project(monkeypatch):
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    with pytest.raises(EmbedError, match="GOOGLE_CLOUD_PROJECT"):
        VertexEmbedder()


def test_settings_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("POST_DESK_EMBED_MODEL", "gemini-embedding-2")
    monkeypatch.setenv("POST_DESK_EMBED_DIMS", "1536")
    e = VertexEmbedder(client=FakeGoogle())
    assert (e.name, e.dims) == ("gemini-embedding-2", 1536)


def test_empty_input():
    assert VertexEmbedder(dims=4, client=FakeGoogle()).embed([], DOC).shape == (0, 4)
