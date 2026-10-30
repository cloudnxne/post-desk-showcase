"""Connecting gets its own short limit (the cause of the 77 to 116 second runs: a dead address held each new
connection for the whole cut-off before the next address was tried)."""
import httpx
import pytest

from app.llm import CONNECT_TIMEOUT, AnthropicModel, GeminiModel, connect_capped_httpx_client


def test_claude_client_connects_within_5_seconds():
    t = AnthropicModel(api_key="k")._client.timeout
    assert (t.connect, t.read) == (CONNECT_TIMEOUT, AnthropicModel.ATTEMPT_TIMEOUT) and CONNECT_TIMEOUT == 5


def test_short_attempts_keep_the_connect_limit():
    m = AnthropicModel(api_key="k")
    t = m._timeout(60)
    assert (t.connect, t.read) == (5, 60)


def test_google_sdk_timeouts_are_split_into_connect_and_reply():
    c = connect_capped_httpx_client()
    req = c.build_request("POST", "https://aiplatform.googleapis.com/x", timeout=70.0)   # what Google's SDK passes
    t = req.extensions["timeout"]
    assert t["connect"] == 5 and t["read"] == 70
    tiny = c.build_request("GET", "https://x.example/", timeout=2.0)
    assert tiny.extensions["timeout"]["connect"] == 2           # never longer than the whole timeout


def test_gemini_uses_the_capped_client(monkeypatch):
    m = GeminiModel(project="my-project", location="global")
    hc = m._client._api_client._httpx_client
    req = hc.build_request("POST", "https://aiplatform.googleapis.com/x", timeout=70.0)
    assert req.extensions["timeout"]["connect"] == 5


def test_a_dead_address_costs_seconds_not_the_whole_attempt(monkeypatch):
    """Simulates the real failure: the first address never answers, the second works."""
    import socket
    tried = []

    def fake_create_connection(address, timeout=None, *a, **kw):
        tried.append(timeout)
        raise socket.timeout("timed out")       # stands in for the dead first address
    c = connect_capped_httpx_client()
    monkeypatch.setattr(socket, "create_connection", fake_create_connection)
    with pytest.raises(httpx.ConnectTimeout):
        c.post("https://198.51.100.1/x", timeout=70.0)
    assert tried and all(t == 5 for t in tried)
