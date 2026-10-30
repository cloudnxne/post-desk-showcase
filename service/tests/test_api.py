from app.llm import ModelError
from conftest import FIRST_ANSWER, KNOWN, RETRY_ANSWER, body


def test_health(api):
    client, _ = api
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["ok"] is True


# ---------- auth ----------
def test_rejects_missing_key(api):
    client, set_model = api
    set_model(FIRST_ANSWER, RETRY_ANSWER)
    r = client.post("/generate", json=body(), headers={"X-API-Key": ""})
    assert r.status_code == 401


def test_rejects_wrong_key(api):
    client, set_model = api
    set_model(FIRST_ANSWER, RETRY_ANSWER)
    assert client.post("/generate", json=body(), headers={"X-API-Key": "nope"}).status_code == 401


def test_refuses_everything_when_no_key_is_configured(api, monkeypatch):
    client, set_model = api
    set_model(FIRST_ANSWER)
    monkeypatch.delenv("POST_DESK_API_KEY")
    assert client.post("/generate", json=body()).status_code == 503


# ---------- validation ----------
def test_rejects_empty_post(api):
    client, set_model = api
    set_model(FIRST_ANSWER)
    b = body()
    b["post"]["content"] = ""
    assert client.post("/generate", json=b).status_code == 422


def test_rejects_empty_sitemap(api):
    client, set_model = api
    set_model(FIRST_ANSWER)
    assert client.post("/generate", json=body(sitemap_urls=[])).status_code == 422


def test_rejects_bad_priority_page(api):
    client, set_model = api
    set_model(FIRST_ANSWER)
    b = body()
    b["client"]["priority_pages"] = ["not a url"]
    assert client.post("/generate", json=b).status_code == 422


# ---------- the generate run ----------
def run(api, *answers, **over):
    client, set_model = api
    model = set_model(*answers)
    r = client.post("/generate", json=body(**over))
    assert r.status_code == 200, r.text
    return r.json(), model


def test_only_verified_links_come_back_in_post_order(api):
    out, model = run(api, FIRST_ANSWER, RETRY_ANSWER)
    assert [l["anchor_text"] for l in out["internal_links"]] == ["garden design service", "patio installation", "Artificial grass"]
    assert all(l["verified"] for l in out["internal_links"])
    assert out["internal_links"][1]["sentence"] == "We handle patio installation from groundwork to pointing."


def test_bad_anchor_is_retried_once_and_off_sitemap_url_is_not(api):
    out, model = run(api, FIRST_ANSWER, RETRY_ANSWER)
    assert len(model.prompts) == 2
    assert 'rejected anchor: "fake grass lawns"' in model.prompts[1]
    assert "competitor.com" not in model.prompts[1]
    assert out["stats"] == {**out["stats"], "suggested": 4, "verified": 3, "dropped": 1, "retried": 1, "model": "fake-model"}


def test_no_retry_when_every_anchor_is_good(api):
    answer = {**FIRST_ANSWER, "internal_links": FIRST_ANSWER["internal_links"][:2]}
    out, model = run(api, answer)
    assert len(model.prompts) == 1
    assert out["stats"]["retried"] == 0


def test_a_failed_retry_keeps_the_first_pass(api):
    out, model = run(api, FIRST_ANSWER, ModelError("rate_limited", "busy"))
    assert [l["anchor_text"] for l in out["internal_links"]] == ["garden design service", "patio installation"]


def test_meta_lengths_are_measured_and_empty_ones_dropped(api):
    out, _ = run(api, FIRST_ANSWER, RETRY_ANSWER)
    assert [(m["length"], m["status"]) for m in out["meta_descriptions"]] == [(150, "good"), (170, "may truncate"), (9, "short")]


def test_odd_model_values_are_tidied(api):
    out, _ = run(api, FIRST_ANSWER, RETRY_ANSWER)
    assert out["internal_links"][0]["priority"] == "medium"          # "urgent" isn't allowed
    assert [h["level"] for h in out["headings"]] == [2, 6]            # level 9 clamped, missing text dropped
    assert out["lists"] == [{"type": "ul", "items": ["Artificial grass: stays green all year", "Gravel borders"]}]
    assert out["title_status"] == "good"


def test_backlinks_must_come_from_the_sitemap(api):
    out, _ = run(api, FIRST_ANSWER, RETRY_ANSWER)
    assert [b["source_url"] for b in out["link_back_from"]] == [KNOWN[3]]


def test_post_never_links_to_its_own_url(api):
    b = body()
    b["post"]["url"] = KNOWN[1]
    client, set_model = api
    model = set_model(FIRST_ANSWER, RETRY_ANSWER)
    out = client.post("/generate", json=b).json()
    candidates = model.prompts[0].split("EXISTING SITE URLS")[1].split("POST CONTENT")[0]
    assert KNOWN[1] not in candidates
    assert all("patio-installation" not in l["target_url"] for l in out["internal_links"])


def test_priority_pages_lead_the_candidate_list(api):
    _, model = run(api, FIRST_ANSWER, RETRY_ANSWER)
    urls = model.prompts[0].split("EXISTING SITE URLS")[1].split("POST CONTENT")[0].strip().splitlines()[1:]
    assert urls[0] == KNOWN[0]
    assert len(urls) == len(set(urls))


def test_large_sitemaps_are_cut_to_350_candidates(api):
    big = KNOWN + [f"https://example-landscapes.co.uk/misc/page-{i}/" for i in range(1000)]
    out, model = run(api, FIRST_ANSWER, RETRY_ANSWER, sitemap_urls=big)
    assert out["stats"]["candidates"] <= 351
    assert "of 1004, pre-filtered by relevance" in model.prompts[0]
    assert KNOWN[1] in model.prompts[0]   # the relevant page survives the cut


def test_keyword_falls_back_to_the_models(api):
    out, _ = run(api, FIRST_ANSWER, RETRY_ANSWER)
    assert out["primary_keyword"] == "garden design"


# ---------- model failures ----------
def test_malformed_model_answer_is_a_502(api):
    client, set_model = api
    set_model(ModelError("invalid_json", "bad"))
    r = client.post("/generate", json=body())
    assert r.status_code == 502 and r.json()["detail"]["code"] == "invalid_json"


def test_rate_limit_is_a_429(api):
    client, set_model = api
    set_model(ModelError("rate_limited", "slow down"))
    assert client.post("/generate", json=body()).status_code == 429


def test_model_timeout_is_a_504(api):
    client, set_model = api
    set_model(ModelError("model_timeout", "slow"))
    r = client.post("/generate", json=body())
    assert r.status_code == 504 and r.json()["detail"]["code"] == "model_timeout"


def test_start_of_each_run_is_logged(api, caplog):
    client, set_model = api
    set_model(FIRST_ANSWER, RETRY_ANSWER)
    client.post("/generate", json=body())
    assert '"event": "generate_start"' in caplog.text and '"model": "fake-model"' in caplog.text
