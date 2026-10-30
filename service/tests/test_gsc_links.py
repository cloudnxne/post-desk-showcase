"""Ranking data in link picks: close-to-page-one pages reach Claude, with position, impressions and top searches."""
import pytest

from app import generate as gen
from app.embed import HashEmbedder
from app.generate import generate
from app.gsc import SearchConsole, SearchConsoleError
from app.main import app, get_search
from app.prompts import SEARCH_RULES
from app.store import Index, LocalStore
from conftest import FIRST_ANSWER, RETRY_ANSWER, body
from test_gsc import FakeSession, row

SITE = "https://www.example.co.uk"
DOMAIN = "example.co.uk"
PAGES = {
    SITE + "/services/artificial-grass-installation/": "Artificial grass installation service with sub-base, membrane and laying",
    SITE + "/latest-news/cleaning-artificial-grass/": "Cleaning artificial grass with pet safe cleaner and brushing",
    SITE + "/services/patio-installation/": "Patio installation with natural stone slabs",
    SITE + "/your-quote/": "Get a free quote for your garden",
}
POST = ("Artificial grass needs a little care. Brush it and use a pet safe cleaner. "
        "A good installation with a proper sub-base lasts for years. Get a free quote today.")


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


def search(rows):
    return SearchConsole(session=FakeSession([{"siteUrl": f"sc-domain:{DOMAIN}", "permissionLevel": "siteOwner"}], rows), who="test")


ROWS = [row(SITE + "/services/artificial-grass-installation/", "artificial grass installation", 1800, 11.0),
        row(SITE + "/services/artificial-grass-installation/", "artificial grass fitters", 400, 14.0),
        row(SITE + "/latest-news/cleaning-artificial-grass/", "how to clean artificial grass", 900, 2.5)]


def req():
    return {"client": {"name": "C", "domain": DOMAIN}, "post": {"title": "Caring for artificial grass", "keyword": "", "url": "", "content": POST},
            "sitemap_urls": list(PAGES)}


def run(store, search_console, answer=None):
    m = Model(answer or {"internal_links": [{"anchor_text": "good installation", "target_url": SITE + "/services/artificial-grass-installation/",
                                             "priority": "high"}]})
    out = generate(req(), m, store=store, embedder=HashEmbedder(), search=search_console)
    return out, m.prompts[0]


def test_close_pages_are_marked_for_claude_with_their_searches(store):
    out, p = run(store, search(ROWS))
    assert 'Google: average position 11.5, 2,200 impressions, CLOSE TO PAGE ONE; top searches: "artificial grass installation", "artificial grass fitters"' in p
    assert SEARCH_RULES.strip() in p


def test_pages_already_on_page_one_show_data_but_not_the_flag(store):
    _, p = run(store, search(ROWS))
    assert 'Google: average position 2.5, 900 impressions; top searches: "how to clean artificial grass"' in p


def test_stats_report_what_was_offered_and_linked(store):
    out, _ = run(store, search(ROWS))
    assert out["stats"]["search_console"] == {"property": f"sc-domain:{DOMAIN}", "close_to_page_one_offered": 1, "close_to_page_one_linked": 1}


def test_relevant_close_pages_are_brought_in_beyond_the_usual_cut(store, monkeypatch):
    monkeypatch.setattr(gen, "SEMANTIC_LIMIT", 1)         # only the single best match by meaning would normally get through
    rows = [row(SITE + "/services/patio-installation/", "patio installation", 700, 12.0)]
    _, p = run(store, search(rows))
    assert SITE + "/services/patio-installation/" in p.split("POST CONTENT")[0]


def test_no_rules_when_nothing_is_close(store):
    _, p = run(store, search([row(SITE + "/latest-news/cleaning-artificial-grass/", "clean artificial grass", 900, 2.5)]))
    assert SEARCH_RULES.strip() not in p and "Google: average position 2.5" in p


def test_search_console_failing_never_stops_a_post(store):
    out, p = run(store, SearchConsole(session=FakeSession([], status=403), who="service account x"))
    assert "Google:" not in p and out["stats"]["retrieval"] == "semantic"
    assert out["stats"]["search_console"]["error"].startswith("no_access")


def test_without_search_console_nothing_changes(store):
    out, p = run(store, None)
    assert "Google:" not in p and out["stats"]["search_console"] is None


def test_url_only_path_keeps_the_page_prompt_exactly(tmp_path):
    m = Model({"internal_links": []})
    generate(req(), m, store=LocalStore(tmp_path), embedder=HashEmbedder(), search=search(ROWS))
    assert "EXISTING SITE URLS" in m.prompts[0] and "Google:" not in m.prompts[0]


# ---------- endpoints ----------
@pytest.fixture
def with_search(api):
    client, set_model = api
    sc = search(ROWS + [row(SITE + "/services/patio-installation/", "patio installation", 700, 12.0)])
    app.dependency_overrides[get_search] = lambda: sc
    return client


def test_properties_endpoint(with_search):
    r = with_search.get("/search/properties").json()
    assert r == {"account": "test", "properties": [{"property": f"sc-domain:{DOMAIN}", "permission": "siteOwner"}]}


def test_opportunities_endpoint(with_search):
    r = with_search.get(f"/search/opportunities/https://www.{DOMAIN}").json()
    assert r["property"] == f"sc-domain:{DOMAIN}" and r["close_to_page_one"] == 2
    assert [p["url"] for p in r["pages"]] == [SITE + "/services/artificial-grass-installation/", SITE + "/services/patio-installation/"]
    assert r["pages"][0]["top_searches"][0] == "artificial grass installation"


def test_opportunities_for_an_unknown_client_is_404(with_search):
    assert with_search.get("/search/opportunities/nobody.co.uk").status_code == 404


def test_endpoints_need_sign_in(with_search):
    assert with_search.get("/search/properties", headers={"X-API-Key": "wrong"}).status_code == 401


def test_endpoints_explain_when_not_set_up(api):
    client, _ = api
    assert client.get("/search/properties").status_code == 503
