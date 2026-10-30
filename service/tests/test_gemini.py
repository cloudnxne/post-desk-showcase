"""Gemini on Vertex AI and Claude on Vertex AI, with stand-ins for the Google and Anthropic clients (no network)."""
from types import SimpleNamespace

import httpx
import pytest
from google.genai import errors

from app import llm
from app.llm import ClaudeVertexModel, GeminiModel, ModelError, make_model
from app.main import app, model_for
from conftest import FIRST_ANSWER, RETRY_ANSWER, body


class FakeGoogle:
    """Plays back a script of outcomes for generate_content and records each call."""

    def __init__(self, *script):
        self.script, self.calls = list(script), []
        self.models = self

    def generate_content(self, model, contents, config):
        self.calls.append(SimpleNamespace(model=model, contents=contents, config=config))
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        text = '{"ok": true}' if step == "ok" else step
        usage = SimpleNamespace(candidates_token_count=200, thoughts_token_count=300, prompt_token_count=5000)
        return SimpleNamespace(text=text, usage_metadata=usage)


def api_error(code):
    return errors.APIError(code, {"error": {"message": f"error {code}", "status": "X"}})


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)


def gemini(*script, **kw):
    g = FakeGoogle(*script)
    return GeminiModel(model="gemini-3.8-flash", client=g, **kw), g


# ---------- the request Gemini gets ----------
def test_asks_for_json_with_a_thinking_level_and_a_timeout(monkeypatch):
    monkeypatch.delenv("POST_DESK_EFFORT", raising=False)
    m, g = gemini("ok")
    out, attempts = m.json_with_info("prompt text")
    c = g.calls[0]
    assert out == {"ok": True} and c.model == "gemini-3.8-flash" and c.contents == "prompt text"
    assert c.config.response_mime_type == "application/json"
    assert str(c.config.thinking_config.thinking_level).endswith("LOW")
    assert c.config.http_options.timeout == GeminiModel.ATTEMPT_TIMEOUT * 1000
    assert attempts == [{"ms": attempts[0]["ms"], "outcome": "ok", "effort": "low", "provider": "gemini", "output_tokens": 500}]


@pytest.mark.parametrize("effort, level", [("low", "LOW"), ("medium", "MEDIUM"), ("high", "HIGH")])
def test_effort_maps_to_thinking_level(effort, level):
    m, g = gemini("ok")
    m.json_with_info("p", effort=effort)
    assert str(g.calls[0].config.thinking_config.thinking_level).endswith(level)


def test_short_timeout_for_the_anchor_fix_up():
    m, g = gemini("ok")
    m.json_with_info("p", attempts=1, timeout=60, effort="low")
    assert g.calls[0].config.http_options.timeout == 60000


def test_reads_json_even_with_a_code_fence():
    m, _ = gemini('```json\n{"a": 1}\n```')
    assert m.json("p") == {"a": 1}


# ---------- failures ----------
@pytest.mark.parametrize("first, outcome", [
    (api_error(429), "rate_limited"), (api_error(503), "server_error"),
    (httpx.ReadTimeout("slow"), "timeout"), (httpx.ConnectError("refused"), "connection_error"),
])
def test_temporary_failures_are_retried_and_recorded(first, outcome):
    m, g = gemini(first, "ok")
    out, attempts = m.json_with_info("p")
    assert out == {"ok": True} and [a["outcome"] for a in attempts] == [outcome, "ok"] and len(g.calls) == 2


def test_permission_or_model_errors_fail_straight_away_with_the_reason():
    m, g = gemini(api_error(404))
    with pytest.raises(ModelError) as e:
        m.json_with_info("p")
    assert e.value.code == "model_error" and "404" in str(e.value) and len(g.calls) == 1


def test_two_timeouts_give_a_clear_error():
    m, _ = gemini(httpx.ReadTimeout("slow"), httpx.ReadTimeout("slow"))
    with pytest.raises(ModelError) as e:
        m.json_with_info("p")
    assert e.value.code == "model_timeout"


def test_empty_answer_is_an_error():
    m, _ = gemini("")
    with pytest.raises(ModelError, match="empty"):
        m.json("p")


def test_needs_a_project(monkeypatch):
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    with pytest.raises(RuntimeError, match="GOOGLE_CLOUD_PROJECT"):
        GeminiModel()


def test_settings_from_the_environment(monkeypatch):
    monkeypatch.setenv("POST_DESK_GEMINI_MODEL", "gemini-3.1-pro")
    monkeypatch.setenv("POST_DESK_EFFORT", "low")
    m = GeminiModel(client=FakeGoogle())
    assert (m.name, m.effort, m.provider) == ("gemini-3.1-pro", "low", "gemini")


# ---------- Claude on Vertex ----------
def test_claude_on_vertex_uses_the_vertex_client(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "my-project")
    made = {}

    class FakeVertex:
        def __init__(self, **kw):
            made.update(kw)
            self.messages = None
    import anthropic
    monkeypatch.setattr(anthropic, "AnthropicVertex", FakeVertex)
    m = ClaudeVertexModel()
    assert m.provider == "claude-vertex" and made["project_id"] == "my-project" and made["region"] == "global"
    assert made["max_retries"] == 0


def test_unknown_provider():
    with pytest.raises(RuntimeError, match="Unknown model provider"):
        make_model("gpt")


# ---------- choosing the model per request ----------
class Named:
    def __init__(self, provider, *answers):
        self.provider, self.name, self.answers, self.prompts = provider, provider + "-model", list(answers), []

    def json(self, prompt):
        self.prompts.append(prompt)
        return self.answers.pop(0)


def test_request_can_pick_gemini(api, monkeypatch):
    client, set_model = api
    set_model(FIRST_ANSWER, RETRY_ANSWER)          # the default (Claude stand-in)
    gem = Named("gemini", FIRST_ANSWER, RETRY_ANSWER)
    monkeypatch.setattr("app.main.model_for", lambda p: gem)
    b = body()
    b["options"] = {"provider": "gemini", "effort": "low"}
    r = client.post("/generate", json=b)
    assert r.status_code == 200, r.text
    assert r.json()["stats"]["provider"] == "gemini" and r.json()["stats"]["model"] == "gemini-model"
    assert len(gem.prompts) == 2


def test_default_provider_is_used_when_not_asked(api):
    client, set_model = api
    set_model(FIRST_ANSWER, RETRY_ANSWER)
    assert client.post("/generate", json=body()).json()["stats"]["model"] == "fake-model"


def test_unavailable_provider_is_a_clear_400(api, monkeypatch):
    client, set_model = api
    set_model(FIRST_ANSWER)
    model_for.cache_clear()
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    b = body()
    b["options"] = {"provider": "gemini"}
    r = client.post("/generate", json=b)
    assert r.status_code == 400 and r.json()["detail"]["code"] == "provider_unavailable"
    model_for.cache_clear()


def test_unknown_provider_is_rejected(api):
    client, set_model = api
    set_model(FIRST_ANSWER)
    b = body()
    b["options"] = {"provider": "gpt"}
    assert client.post("/generate", json=b).status_code == 422
