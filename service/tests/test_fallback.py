"""Claude first, Gemini if Claude fails."""
import pytest

from app.llm import FallbackModel, ModelError
from app.main import app, get_model, model_for
from conftest import FIRST_ANSWER, RETRY_ANSWER, body


class Scripted:
    """A model stand-in that plays back answers or errors, and records how it was called."""

    def __init__(self, provider, *script):
        self.provider, self.name, self.effort = provider, provider + "-model", "low"
        self.script, self.calls = list(script), []

    def json_with_info(self, prompt, attempts=None, timeout=None, effort=None):
        self.calls.append({"prompt": prompt[:30], "attempts": attempts, "timeout": timeout, "effort": effort})
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step, [{"ms": 5, "outcome": "ok", "provider": self.provider}]


DOWN = ModelError("model_timeout", "The model took too long to answer.")


# ---------- the wrapper ----------
def test_main_model_answers_when_healthy():
    claude, gemini = Scripted("anthropic", {"a": 1}), Scripted("gemini")
    m = FallbackModel(claude, gemini)
    out, log = m.json_with_info("p")
    assert out == {"a": 1} and m.provider == "anthropic" and m.fell_back is None and gemini.calls == []
    assert claude.calls[0]["timeout"] == FallbackModel.PRIMARY_TIMEOUT


def test_fallback_answers_when_main_fails():
    claude, gemini = Scripted("anthropic", DOWN), Scripted("gemini", {"b": 2})
    m = FallbackModel(claude, gemini)
    out, log = m.json_with_info("p", effort="low")
    assert out == {"b": 2} and m.provider == "gemini" and m.name == "gemini-model"
    assert m.fell_back.startswith("anthropic model_timeout")
    assert [x["outcome"] for x in log] == ["fell_back", "ok"]
    assert gemini.calls == [{"prompt": "p", "attempts": 1, "timeout": 60, "effort": "low"}]


def test_after_a_fallback_the_rest_of_the_run_stays_on_the_fallback():
    claude, gemini = Scripted("anthropic", DOWN), Scripted("gemini", {"main": 1}, {"fix": 1})
    m = FallbackModel(claude, gemini)
    m.json_with_info("main prompt")
    out, _ = m.json_with_info("fix prompt", attempts=1, timeout=60, effort="low")
    assert out == {"fix": 1} and len(claude.calls) == 1        # Claude isn't asked again in this run


@pytest.mark.parametrize("code", ["model_timeout", "rate_limited", "model_error", "invalid_json"])
def test_any_failure_triggers_the_fallback(code):
    m = FallbackModel(Scripted("anthropic", ModelError(code, "x")), Scripted("gemini", {"ok": 1}))
    assert m.json("p") == {"ok": 1}


def test_both_failing_gives_one_clear_error():
    m = FallbackModel(Scripted("anthropic", DOWN), Scripted("gemini", ModelError("rate_limited", "busy")))
    with pytest.raises(ModelError) as e:
        m.json("p")
    assert e.value.code == "model_timeout" and "Both models failed" in str(e.value) and "gemini rate_limited" in str(e.value)


def test_forced_failure_skips_the_main_model():
    claude, gemini = Scripted("anthropic", {"a": 1}), Scripted("gemini", {"b": 2})
    m = FallbackModel(claude, gemini, force_failure=True)
    assert m.json("p") == {"b": 2} and claude.calls == []


def test_worst_case_fits_inside_cloud_run():
    from app.llm import AnthropicModel
    main = AnthropicModel.ATTEMPTS * FallbackModel.PRIMARY_TIMEOUT + FallbackModel.FALLBACK_TIMEOUT
    fix_up = FallbackModel.FALLBACK_TIMEOUT          # after a fallback, the fix-up goes straight to Gemini
    assert main + fix_up + 30 < 300                  # 30s for retrieval and the rest


# ---------- through the API ----------
@pytest.fixture
def two_models(api, monkeypatch):
    client, _ = api
    holder = {}

    def setup(claude_script, gemini_script):
        holder["claude"] = Scripted("anthropic", *claude_script)
        holder["gemini"] = Scripted("gemini", *gemini_script)
        app.dependency_overrides[get_model] = lambda: holder["claude"]
        monkeypatch.setattr("app.main.model_for", lambda p: holder["gemini"] if p == "gemini" else holder["claude"])
        return holder["claude"], holder["gemini"]
    return client, setup


def test_api_reports_the_fallback(two_models):
    client, setup = two_models
    claude, gemini = setup([DOWN], [FIRST_ANSWER, RETRY_ANSWER])
    s = client.post("/generate", json=body()).json()["stats"]
    assert s["provider"] == "gemini" and s["model"] == "gemini-model" and s["fell_back"].startswith("anthropic model_timeout")


def test_api_healthy_run_has_no_fallback(two_models):
    client, setup = two_models
    setup([FIRST_ANSWER, RETRY_ANSWER], [])
    s = client.post("/generate", json=body()).json()["stats"]
    assert s["provider"] == "anthropic" and s["fell_back"] is None


def test_naming_a_provider_turns_the_fallback_off(two_models):
    client, setup = two_models
    claude, gemini = setup([DOWN], [FIRST_ANSWER])
    b = body()
    b["options"] = {"provider": "anthropic"}
    r = client.post("/generate", json=b)
    assert r.status_code == 504 and gemini.calls == []


def test_fallback_can_be_switched_off(two_models, monkeypatch):
    client, setup = two_models
    setup([DOWN], [FIRST_ANSWER])
    monkeypatch.setenv("POST_DESK_FALLBACK_PROVIDER", "none")
    assert client.post("/generate", json=body()).status_code == 504


def test_force_fallback_works_for_the_api_key(two_models):
    client, setup = two_models
    claude, gemini = setup([FIRST_ANSWER, RETRY_ANSWER], [FIRST_ANSWER, RETRY_ANSWER])
    b = body()
    b["options"] = {"force_fallback": True}
    s = client.post("/generate", json=b).json()["stats"]
    assert s["provider"] == "gemini" and "forced failure" in s["fell_back"] and claude.calls == []


def test_unavailable_fallback_leaves_the_main_model_working(two_models, monkeypatch):
    client, setup = two_models
    setup([FIRST_ANSWER, RETRY_ANSWER], [])

    def broken(p):
        raise RuntimeError("no Google project")
    monkeypatch.setattr("app.main.model_for", broken)
    s = client.post("/generate", json=body()).json()["stats"]
    assert s["provider"] == "anthropic"
