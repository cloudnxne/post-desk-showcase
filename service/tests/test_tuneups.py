"""The tune-ups from the first real run on lawnco.example.co.uk: link count by length, trimming, page types,
contact pages, the link-mix and anchor rules, stage timings and model attempts."""
from types import SimpleNamespace

import anthropic
import httpx
import pytest

from app import crawl, indexer
from app.embed import HashEmbedder
from app.generate import generate
from app.llm import AnthropicModel, ModelError
from app.prompts import LINK_MIX_RULES, generate_prompt, link_range
from app.retrieve import ARTICLE, CONTACT, MAIN_SERVICE, OTHER, SERVICE, page_kind
from app.store import Index, LocalStore
from app.embed import normalise
import numpy as np

SITE = "https://www.example.co.uk"


# ---------- how many links ----------
@pytest.mark.parametrize("words, expected", [
    (60, (1, 2)), (120, (1, 2)), (300, (1, 3)), (600, (3, 6)), (1000, (5, 10)), (3000, (10, 10)),
])
def test_link_range_follows_post_length(words, expected):
    assert link_range(words) == expected


def test_prompt_asks_for_the_range():
    p = generate_prompt({"name": "c"}, {"content": "x"}, ["u"], 1, [], links=(1, 2))
    assert "3. Find 1–2 internal linking opportunities" in p
    p = generate_prompt({"name": "c"}, {"content": "x"}, ["u"], 1, [], links=(10, 10))
    assert "3. Find 10 internal linking opportunities" in p


class Fake:
    name = "fake"

    def __init__(self, *answers):
        self.answers, self.prompts, self.kwargs = list(answers), [], []

    def json_with_info(self, prompt, **kw):
        self.prompts.append(prompt)
        self.kwargs.append(kw)
        return self.answers.pop(0), [{"ms": 5, "outcome": "ok"}]


SHORT_POST = ("We fit artificial grass and lay patios. Our team also builds decking and fencing. "
              "Ask for a free quote and we will visit.")   # about 25 words: 1 to 2 links


def req(content=SHORT_POST, urls=None, domain=""):
    urls = urls or [SITE + p for p in ("/artificial-grass/", "/patios/", "/decking/", "/fencing/", "/contact-us/")]
    return {"client": {"name": "C", "domain": domain}, "post": {"title": "", "keyword": "", "url": "", "content": content},
            "sitemap_urls": urls}


def test_extra_links_are_trimmed_keeping_high_priority():
    answer = {"internal_links": [
        {"anchor_text": "artificial grass", "target_url": SITE + "/artificial-grass/", "priority": "medium"},
        {"anchor_text": "lay patios", "target_url": SITE + "/patios/", "priority": "high"},
        {"anchor_text": "builds decking", "target_url": SITE + "/decking/", "priority": "low"},
        {"anchor_text": "free quote", "target_url": SITE + "/contact-us/", "priority": "high"},
    ]}
    out = generate(req(), Fake(answer))
    assert [l["anchor_text"] for l in out["internal_links"]] == ["lay patios", "free quote"]   # both high, in post order
    assert out["stats"]["verified"] == 4 and out["stats"]["trimmed"] == 2 and out["stats"]["link_range"] == [1, 2]


def test_long_posts_keep_all_their_links():
    long_post = SHORT_POST + " " + ("More words about gardens and outdoor spaces. " * 150)
    answer = {"internal_links": [{"anchor_text": a, "target_url": SITE + u, "priority": "medium"} for a, u in
                                 [("artificial grass", "/artificial-grass/"), ("lay patios", "/patios/"), ("builds decking", "/decking/")]]}
    out = generate(req(long_post), Fake(answer))
    assert len(out["internal_links"]) == 3 and out["stats"]["trimmed"] == 0


# ---------- page types ----------
@pytest.mark.parametrize("url, title, kind", [
    ("https://lawnco.example.co.uk/latest-news/best-ways-to-clean-artificial-grass/", "", ARTICLE),
    ("https://x.co.uk/blog/how-to-lay-a-patio/", "", ARTICLE),
    ("https://x.co.uk/guides/sub-base/", "", ARTICLE),
    ("https://x.co.uk/contact-us/", "", CONTACT),
    ("https://x.co.uk/get-a-quote/", "", CONTACT),
    ("https://x.co.uk/free-survey/", "", CONTACT),
    ("https://x.co.uk/p/12/", "Get a Free Quote | X", CONTACT),
    ("https://x.co.uk/artificial-grass-installation/", "", MAIN_SERVICE),
    ("https://lawnco.example.co.uk/artificial-grass/", "", MAIN_SERVICE),
    ("https://x.co.uk/services/patios/", "", MAIN_SERVICE),            # one level under a services hub
    ("https://x.co.uk/services/", "", MAIN_SERVICE),
    ("https://lawnco.example.co.uk/artificial-grass/artificial-grass-for-dogs/", "", SERVICE),   # a sub-service
    ("https://x.co.uk/product/supreme-grass-40mm/", "", SERVICE),
    ("https://x.co.uk/areas-covered/maidstone/", "", SERVICE),         # a location page is still a money page
    ("https://x.co.uk/", "", SERVICE),
    ("https://x.co.uk/bookcases/", "", MAIN_SERVICE),      # "book" inside a word isn't a booking page
    ("https://lawnco.example.co.uk/about-us/", "", OTHER),
    ("https://lawnco.example.co.uk/privacy-policy/", "", OTHER),
    ("https://lawnco.example.co.uk/reviews/", "", OTHER),
    ("https://lawnco.example.co.uk/areas-covered/", "", OTHER),
    ("https://lawnco.example.co.uk/our-work/", "", OTHER),
    ("https://x.co.uk/our-work/smith-garden/", "", OTHER),
    ("https://lawnco.example.co.uk/samples/", "", OTHER),
    ("https://x.co.uk/cookie-policy.html", "", OTHER),
])
def test_page_kind(url, title, kind):
    assert page_kind(url, {"title": title}) == kind


# ---------- semantic path: types, contact pages and rules in the prompt ----------
def test_contact_page_is_always_offered_with_types_and_rules(tmp_path, monkeypatch):
    # Only the top 2 pages by meaning get through, and the contact page matches the post least of all.
    monkeypatch.setattr("app.generate.SEMANTIC_LIMIT", 2)
    pages = {
        SITE + "/latest-news/cleaning-artificial-grass/": "Cleaning artificial grass with pet safe cleaner and brushes",
        SITE + "/artificial-grass-installation/": "Artificial grass installation service with sub-base and membrane",
        SITE + "/contact-us/": "Opening hours and phone number for our office",   # nothing like the post
    }
    emb = HashEmbedder()
    idx = Index("example.co.uk", emb.name, emb.dims)
    vecs = emb.embed(list(pages.values()), "RETRIEVAL_DOCUMENT")
    for (u, text), v in zip(pages.items(), vecs):
        idx.pages[u] = {"status": "indexed", "title": text[:40], "summary": text}
        idx.vectors[u] = v
    store = LocalStore(tmp_path)
    store.save(idx)
    model = Fake({"internal_links": []})
    post = "Keep artificial grass clean with a pet safe cleaner. A good installation with a solid sub-base lasts. Get a free quote today."
    generate(req(post, urls=list(pages), domain="example.co.uk"), model, store=store, embedder=HashEmbedder())
    p = model.prompts[0]
    assert "Type: article" in p and "Type: main service page" in p and "Type: contact or quote page" in p
    assert SITE + "/contact-us/" in p.split("POST CONTENT")[0]
    assert LINK_MIX_RULES.strip() in p
    assert 'Avoid generic phrases' in p


def test_url_path_prompt_has_no_extra_rules():
    model = Fake({"internal_links": []})
    generate(req(), model)
    assert "main service page" not in model.prompts[0]


# ---------- timings and model attempts ----------
def test_stats_include_stage_timings_and_attempts():
    answer = {"internal_links": [{"anchor_text": "made up phrase", "target_url": SITE + "/patios/"}]}
    model = Fake(answer, {"internal_links": []})
    out = generate(req(), model)
    s = out["stats"]
    assert set(s["timings"]) == {"retrieval_ms", "model_ms", "anchor_fix_ms"}
    assert s["model_attempts"] == [{"ms": 5, "outcome": "ok"}, {"ms": 5, "outcome": "ok"}]
    assert model.kwargs[1] == {"attempts": 1, "timeout": 60, "effort": "low"}   # the anchor fix-up: one short, low-effort attempt


class FakeMessages:
    def __init__(self, script):
        self.script, self.calls = list(script), []

    def create(self, **kw):
        self.calls.append(kw)
        step = self.script.pop(0)
        if step == "timeout":
            raise anthropic.APITimeoutError(request=httpx.Request("POST", "https://api.anthropic.com"))
        if step == "overloaded":
            raise anthropic.InternalServerError("overloaded", response=httpx.Response(529, request=httpx.Request("POST", "https://x")), body=None)
        return SimpleNamespace(content=[SimpleNamespace(text='{"ok": true}')], usage=SimpleNamespace(output_tokens=42))


def model_with(script, monkeypatch):
    m = AnthropicModel(api_key="k")
    fake = FakeMessages(script)
    monkeypatch.setattr(m._client, "messages", fake)
    monkeypatch.setattr("app.llm.time.sleep", lambda s: None)
    return m, fake


def test_a_stalled_attempt_is_retried_and_recorded(monkeypatch):
    m, fake = model_with(["timeout", "ok"], monkeypatch)
    out, attempts = m.json_with_info("hi")
    assert out == {"ok": True}
    assert [a["outcome"] for a in attempts] == ["timeout", "ok"] and attempts[1]["output_tokens"] == 42
    # 70 seconds for the reply, but only 5 to connect, so one dead address can't eat the whole attempt
    assert all(c["timeout"].read == AnthropicModel.ATTEMPT_TIMEOUT and c["timeout"].connect == 5 for c in fake.calls)


def test_overloaded_is_retried(monkeypatch):
    m, _ = model_with(["overloaded", "ok"], monkeypatch)
    assert [a["outcome"] for a in m.json_with_info("hi")[1]] == ["server_error", "ok"]


def test_two_timeouts_give_a_clear_error(monkeypatch):
    m, _ = model_with(["timeout", "timeout"], monkeypatch)
    with pytest.raises(ModelError) as e:
        m.json_with_info("hi")
    assert e.value.code == "model_timeout"


def test_worst_case_fits_inside_cloud_run():
    # main call (2 attempts) + anchor fix-up (1 attempt of 60s) + a minute for retrieval and the rest
    assert AnthropicModel.ATTEMPTS * AnthropicModel.ATTEMPT_TIMEOUT + 60 + 60 < 300



# ---------- effort ----------
def test_effort_defaults_to_low_and_is_sent(monkeypatch):
    monkeypatch.delenv("POST_DESK_EFFORT", raising=False)
    m, fake = model_with(["ok"], monkeypatch)
    _, attempts = m.json_with_info("hi")
    assert fake.calls[0]["output_config"] == {"effort": "low"} and attempts[0]["effort"] == "low"


def test_effort_setting_and_per_call_override(monkeypatch):
    monkeypatch.setenv("POST_DESK_EFFORT", "low")
    m, fake = model_with(["ok", "ok"], monkeypatch)
    m.json_with_info("hi")
    m.json_with_info("hi", effort="high")
    assert [c["output_config"]["effort"] for c in fake.calls] == ["low", "high"]


def test_request_can_choose_effort():
    model = Fake({"internal_links": []})
    r = req()
    r["options"] = {"effort": "low"}
    out = generate(r, model)
    assert model.kwargs[0] == {"effort": "low"} and out["stats"]["effort"] == "low"


def test_api_rejects_unknown_effort(api):
    from conftest import body
    client, set_model = api
    set_model({"internal_links": []})
    b = body()
    b["options"] = {"effort": "max"}
    assert client.post("/generate", json=b).status_code == 422
