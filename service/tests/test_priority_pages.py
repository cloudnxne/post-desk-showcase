"""Money pages come first: the client's priority pages, then main service pages, then other service pages, then
articles. Search Console's close-to-page-one flag only breaks ties between pages of the same type."""
import pytest

from app.embed import HashEmbedder
from app.generate import generate
from app.prompts import LINK_MIX_RULES, SEARCH_RULES
from app.store import Index, LocalStore
from test_gsc import FakeSession, row
from app.gsc import SearchConsole

SITE = "https://www.example.co.uk"
DOMAIN = "example.co.uk"
MAIN = SITE + "/artificial-grass-installation/"
SUB = SITE + "/artificial-grass/artificial-grass-for-dogs/"
ARTICLE = SITE + "/latest-news/what-sub-base-should-i-use/"
ARTICLE2 = SITE + "/latest-news/cleaning-artificial-grass/"
ABOUT = SITE + "/about-us/"
PAGES = {
    ARTICLE: "Which sub-base to use under artificial grass, compacted stone and a weed membrane",
    ARTICLE2: "Cleaning artificial grass with pet safe cleaner and brushing the fibres",
    SUB: "Artificial grass for dogs and pets, drains well and easy to rinse",
    ABOUT: "About our family business and our team",
    MAIN: "Opening times, phone numbers and directions",   # matches the post least of all
}
POST = ("Artificial grass needs a little care. Brush it and use a pet safe cleaner if you have dogs. "
        "A good installation with a properly compacted sub-base lasts for years. Our family business has done this for decades.")


class Model:
    name, provider, effort = "fake", "anthropic", "low"

    def __init__(self, *answers):
        self.answers, self.prompts = list(answers), []

    def json(self, prompt):
        self.prompts.append(prompt)
        return self.answers.pop(0)


@pytest.fixture
def store(tmp_path):
    emb = HashEmbedder()
    idx = Index(DOMAIN, emb.name, emb.dims)
    for (u, text), v in zip(PAGES.items(), emb.embed(list(PAGES.values()), "RETRIEVAL_DOCUMENT")):
        idx.pages[u] = {"status": "indexed", "title": text[:40], "summary": text}
        idx.vectors[u] = v
    s = LocalStore(tmp_path)
    s.save(idx)
    return s


def search():
    rows = [row(ARTICLE, "sub base for artificial grass", 8800, 9.6), row(ARTICLE2, "how to clean artificial grass", 4000, 12.0)]
    return SearchConsole(session=FakeSession([{"siteUrl": f"sc-domain:{DOMAIN}", "permissionLevel": "siteOwner"}], rows), who="test")


def req(priority=()):
    return {"client": {"name": "C", "domain": DOMAIN, "priority_pages": list(priority)},
            "post": {"title": "", "keyword": "", "url": "", "content": POST}, "sitemap_urls": list(PAGES)}


def link(anchor, url, priority="high"):
    return {"anchor_text": anchor, "target_url": url, "priority": priority}


def run(store, answer, priority=(), sc=None, monkeypatch=None, hi=None):
    if monkeypatch and hi:
        monkeypatch.setattr("app.generate.link_range", lambda words: (1, hi))
    m = Model(answer)
    out = generate(req(priority), m, store=store, embedder=HashEmbedder(), search=sc)
    return out, m.prompts[0]


def test_main_service_pages_are_always_offered(store, monkeypatch):
    monkeypatch.setattr("app.generate.SEMANTIC_LIMIT", 1)     # only the single best match by meaning gets in...
    _, p = run(store, {"internal_links": []})
    listed = p.split("POST CONTENT")[0]
    assert MAIN in listed and "Type: main service page" in listed   # ...but the main service page is still offered
    assert ABOUT not in listed


def test_prompt_sets_the_order_and_search_only_breaks_ties(store):
    _, p = run(store, {"internal_links": []}, sc=search())
    assert LINK_MIX_RULES.strip() in p and SEARCH_RULES.strip() in p
    assert "the PRIORITY PAGES above, then main service pages, then other service or product pages, then articles" in p
    assert "never pick an article over a relevant priority or main service page" in p


def test_priority_pages_are_flagged_in_the_page_list(store):
    _, p = run(store, {"internal_links": []}, priority=[SUB])
    listed = p.split("EXISTING SITE PAGES", 1)[1]
    entry = listed.split(SUB + "\n", 1)[1].split("\n")[0]
    assert entry.strip() == "Type: service or product page, PRIORITY PAGE"


def test_trimming_keeps_main_service_over_a_close_to_page_one_article(store, monkeypatch):
    answer = {"internal_links": [link("properly compacted sub-base", ARTICLE), link("good installation", MAIN)]}
    out, _ = run(store, answer, sc=search(), monkeypatch=monkeypatch, hi=1)
    assert [l["target_url"] for l in out["internal_links"]] == [MAIN]
    assert out["stats"]["trimmed"] == 1


def test_trimming_keeps_the_clients_priority_page_over_a_main_service(store, monkeypatch):
    answer = {"internal_links": [link("good installation", MAIN), link("if you have dogs", SUB)]}
    out, _ = run(store, answer, priority=[SUB], monkeypatch=monkeypatch, hi=1)
    assert [l["target_url"] for l in out["internal_links"]] == [SUB]


def test_between_two_articles_the_close_one_wins(store, monkeypatch):
    rows = [row(ARTICLE, "sub base for artificial grass", 8800, 9.6)]     # only the sub-base article is close,
    # and its anchor comes later in the post, so post order alone would pick the other one
    sc = SearchConsole(session=FakeSession([{"siteUrl": f"sc-domain:{DOMAIN}", "permissionLevel": "siteOwner"}], rows), who="t")
    answer = {"internal_links": [link("properly compacted sub-base", ARTICLE), link("pet safe cleaner", ARTICLE2)]}
    out, _ = run(store, answer, sc=sc, monkeypatch=monkeypatch, hi=1)
    assert [l["target_url"] for l in out["internal_links"]] == [ARTICLE]


def test_a_main_service_page_keeps_its_place_even_when_rated_lower(store, monkeypatch):
    answer = {"internal_links": [link("good installation", MAIN, "low"), link("properly compacted sub-base", ARTICLE, "high")]}
    out, _ = run(store, answer, monkeypatch=monkeypatch, hi=1)
    assert [l["target_url"] for l in out["internal_links"]] == [MAIN]


def test_the_guarantee_takes_only_the_last_place(store, monkeypatch):
    # Two links allowed: the best-rated link stays, and the main service page takes the second place.
    answer = {"internal_links": [link("pet safe cleaner", ARTICLE2, "high"), link("properly compacted sub-base", ARTICLE, "high"),
                                 link("good installation", MAIN, "low")]}
    out, _ = run(store, answer, monkeypatch=monkeypatch, hi=2)
    assert {l["target_url"] for l in out["internal_links"]} == {ARTICLE2, MAIN}


def test_otherwise_the_models_priority_comes_first(store, monkeypatch):
    answer = {"internal_links": [link("pet safe cleaner", ARTICLE2, "low"), link("properly compacted sub-base", ARTICLE, "high")]}
    out, _ = run(store, answer, monkeypatch=monkeypatch, hi=1)
    assert [l["target_url"] for l in out["internal_links"]] == [ARTICLE]


def test_semantic_path_asks_for_spare_links(store, monkeypatch):
    _, p = run(store, {"internal_links": []}, monkeypatch=monkeypatch, hi=2)
    assert "3. Find 1–5 internal linking opportunities" in p


def test_link_mix_shows_what_kind_of_pages_were_linked(store, monkeypatch):
    answer = {"internal_links": [link("good installation", MAIN), link("if you have dogs", SUB), link("pet safe cleaner", ARTICLE2),
                                 link("properly compacted sub-base", ARTICLE), link("family business", ABOUT, "low")]}
    out, _ = run(store, answer, priority=[SUB], sc=search(), monkeypatch=monkeypatch, hi=10)
    assert out["stats"]["link_mix"] == {"priority": 1, "main_service": 1, "service": 0, "contact": 0, "article": 2,
                                        "other": 1, "close_to_page_one": 2}


def test_prompt_names_the_main_service_pages_and_requires_one(store):
    _, p = run(store, {"internal_links": []}, priority=[SUB])
    rule = [line for line in p.splitlines() if line.strip().startswith("- Priority and main service pages here:")]
    assert rule and SUB in rule[0] and MAIN in rule[0] and ARTICLE not in rule[0]
    assert "at least one of your links must go to one of them" in rule[0]


def test_url_path_has_no_must_link_rule():
    m = Model({"internal_links": []})
    generate(req(), m)
    assert "Priority and main service pages here" not in m.prompts[0]


def test_set_aside_shows_what_was_dropped_and_why(store, monkeypatch):
    answer = {"internal_links": [link("good installation", MAIN), link("pet safe cleaner", ARTICLE2, "low"),
                                 link("words not in the post", ARTICLE), link("free quote", SITE + "/not-a-page/")]}
    m = Model(answer, {"internal_links": []})       # the anchor fix-up finds nothing
    monkeypatch.setattr("app.generate.link_range", lambda words: (1, 1))
    out = generate(req(), m, store=store, embedder=HashEmbedder())
    aside = {a["target_url"]: a for a in out["stats"]["set_aside"]}
    assert [l["target_url"] for l in out["internal_links"]] == [MAIN]
    assert aside[ARTICLE2]["why"] == "trimmed to fit the post's length" and aside[ARTICLE2]["type"] == "article"
    assert aside[ARTICLE]["why"].startswith("anchor not found")
    assert aside[SITE + "/not-a-page/"]["why"] == "not in the sitemap"
    assert MAIN not in aside


# ---------- the follow-up when the first answer has no priority or main service link ----------
def test_follow_up_finds_a_main_service_link_and_it_is_kept(store, monkeypatch):
    first = {"internal_links": [link("properly compacted sub-base", ARTICLE), link("pet safe cleaner", ARTICLE2)]}
    follow = {"internal_links": [link("good installation", MAIN)]}
    monkeypatch.setattr("app.generate.link_range", lambda words: (1, 2))
    m = Model(first, follow)
    out = generate(req(), m, store=store, embedder=HashEmbedder())
    assert len(m.prompts) == 2
    asked = m.prompts[1].split("POST CONTENT")[0]
    assert MAIN in asked and SUB not in asked and ARTICLE not in asked and ABOUT not in asked   # SUB is only a sub-service here
    assert MAIN in [l["target_url"] for l in out["internal_links"]] and len(out["internal_links"]) == 2
    assert out["stats"]["main_link_followup"] == "added" and "main_link_ms" in out["stats"]["timings"]


def test_no_follow_up_when_a_main_service_link_is_already_there(store):
    m = Model({"internal_links": [link("good installation", MAIN)]})
    out = generate(req(), m, store=store, embedder=HashEmbedder())
    assert len(m.prompts) == 1 and out["stats"]["main_link_followup"] is None


def test_follow_up_answers_for_other_pages_are_ignored(store):
    m = Model({"internal_links": []}, {"internal_links": [link("pet safe cleaner", ARTICLE2)]})
    out = generate(req(), m, store=store, embedder=HashEmbedder())
    assert out["internal_links"] == [] and out["stats"]["main_link_followup"] == "nothing in the post fits"


def test_follow_up_anchor_must_be_in_the_post(store):
    m = Model({"internal_links": []}, {"internal_links": [link("expert artificial grass fitting", MAIN)]})
    out = generate(req(), m, store=store, embedder=HashEmbedder())
    assert out["internal_links"] == [] and out["stats"]["main_link_followup"] == "anchor not in the post"


def test_a_failed_follow_up_keeps_the_first_answer(store):
    m = Model({"internal_links": [link("pet safe cleaner", ARTICLE2)]})      # no second answer: the follow-up fails
    out = generate(req(), m, store=store, embedder=HashEmbedder())
    assert [l["target_url"] for l in out["internal_links"]] == [ARTICLE2] and out["stats"]["main_link_followup"] == "failed"


def test_no_follow_up_on_the_url_path():
    m = Model({"internal_links": []})
    out = generate(req(), m)
    assert len(m.prompts) == 1 and out["stats"]["main_link_followup"] is None
