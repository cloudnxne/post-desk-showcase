"""options.max_seconds: the Google Doc side panel has a hard 30 second limit, so a run can be asked to finish in time."""
from app import generate as gen
from test_priority_pages import ARTICLE, MAIN, Model, link, req, store   # noqa: F401  (fixture)
from app.embed import HashEmbedder


class Timed(Model):
    """A model that reports how it was called and moves a fake clock forward by `takes` seconds per call."""
    def __init__(self, clock, takes, *answers):
        super().__init__(*answers)
        self.clock, self.takes, self.kwargs = clock, takes, []

    def json_with_info(self, prompt, **kw):
        self.kwargs.append(kw)
        self.clock[0] += self.takes
        return self.json(prompt), [{"ms": int(self.takes * 1000), "outcome": "ok"}]


def run(store, monkeypatch, takes, max_seconds, *answers):
    clock = [1000.0]
    monkeypatch.setattr(gen.time, "monotonic", lambda: clock[0])
    m = Timed(clock, takes, *answers)
    r = req()
    r["options"] = {"max_seconds": max_seconds} if max_seconds else {}
    return gen.generate(r, m, store=store, embedder=HashEmbedder()), m


def test_main_call_fits_inside_the_time_left(store, monkeypatch):
    out, m = run(store, monkeypatch, 5, 25, {"internal_links": [link("good installation", MAIN)]})
    assert m.kwargs[0]["attempts"] == 1 and 15 <= m.kwargs[0]["timeout"] <= 17      # about 65% of 25s


def test_extra_steps_are_skipped_when_time_runs_short(store, monkeypatch):
    # the main call takes 20 of the 25 seconds and finds only an article: no time for the main-service follow-up
    out, m = run(store, monkeypatch, 20, 25, {"internal_links": [link("properly compacted sub-base", ARTICLE)]})
    assert len(m.prompts) == 1
    assert out["stats"]["main_link_followup"] == "skipped: not enough time left"
    assert [l["target_url"] for l in out["internal_links"]] == [ARTICLE]


def test_follow_up_still_runs_when_there_is_time(store, monkeypatch):
    out, m = run(store, monkeypatch, 4, 25, {"internal_links": [link("properly compacted sub-base", ARTICLE)]},
                 {"internal_links": [link("good installation", MAIN)]})
    assert len(m.prompts) == 2 and out["stats"]["main_link_followup"] == "added"
    assert m.kwargs[1]["timeout"] <= 25 - 4 - 3


def test_no_limit_means_no_change(store, monkeypatch):
    out, m = run(store, monkeypatch, 5, None, {"internal_links": [link("good installation", MAIN)]})
    assert "timeout" not in m.kwargs[0] and "attempts" not in m.kwargs[0]


def test_fallback_gets_the_rest_of_the_time(store, monkeypatch):
    from app.llm import FallbackModel, ModelError
    clock = [1000.0]
    monkeypatch.setattr(gen.time, "monotonic", lambda: clock[0])
    seen = {}

    class Slow:
        provider, name, effort = "anthropic", "claude", "low"
        def json_with_info(self, prompt, **kw):
            seen["main"] = kw["timeout"]; clock[0] += kw["timeout"]
            raise ModelError("model_timeout", "slow")

    class Quick(Model):
        provider = "gemini"
        def json_with_info(self, prompt, **kw):
            seen["fallback"] = kw["timeout"]; clock[0] += 3
            return self.json(prompt), [{"ms": 3000, "outcome": "ok"}]

    r = req(); r["options"] = {"max_seconds": 45}
    out = gen.generate(r, FallbackModel(Slow(), Quick({"internal_links": [link("good installation", MAIN)]})),
                       store=store, embedder=HashEmbedder())
    assert 28 <= seen["main"] <= 30 and 12 <= seen["fallback"] <= 16
    assert out["stats"]["fell_back"]


def test_a_timeout_says_where_the_time_went(api, monkeypatch):
    from app.llm import ModelError
    client, set_model = api
    class Late:
        name, provider, effort = "fake", "anthropic", "low"
        def json(self, prompt):
            raise ModelError("model_timeout", "The AI model took too long.")
    from app.main import app, get_model
    app.dependency_overrides[get_model] = lambda: Late()
    from conftest import body
    r = client.post("/generate", json=body())
    assert r.status_code == 504
    d = r.json()["detail"]
    assert "Reading the site and Search Console took" in d["message"] and "model_ms" in d["timings"]
