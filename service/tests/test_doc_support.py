"""What the Google Doc sidebar needs from the API: a client list, posts without a sitemap (the index supplies the
pages), and short index calls that fit Apps Script's time limit."""
import asyncio

import httpx
import pytest

from app import crawl
from app import main as m
from app.crawl import Blocked, find_sitemap
from app.main import app, get_search
from conftest import FIRST_ANSWER, body
from fake_site import ROBOTS, handler, public_dns
from test_semantic_api import BASE, DOMAIN, POST, SITEMAP, index, semantic   # noqa: F401  (fixture)


class FakeSearch:
    def __init__(self, sites):
        self.sites = sites

    def properties(self):
        return self.sites


def test_clients_lists_indexed_domains(semantic):
    client, *_ = semantic
    assert client.get("/clients").json() == {"clients": []}
    index(client)
    assert client.get("/clients").json() == {"clients": [{"domain": DOMAIN, "indexed": True, "search_console": False}]}


def test_clients_include_every_search_console_property(semantic):
    client, *_ = semantic
    index(client)
    app.dependency_overrides[get_search] = lambda: FakeSearch([
        {"siteUrl": "sc-domain:example-landscapes.co.uk", "permissionLevel": "siteOwner"},
        {"siteUrl": "https://www.pavingco.example.co.uk/", "permissionLevel": "siteFullUser"},
        {"siteUrl": "https://pavingco.example.co.uk/", "permissionLevel": "siteFullUser"},      # same client, merged
        {"siteUrl": "https://www.notmine.co.uk/", "permissionLevel": "siteUnverifiedUser"},   # no access: left out
    ])
    assert client.get("/clients").json() == {"clients": [
        {"domain": DOMAIN, "indexed": True, "search_console": True},
        {"domain": "pavingco.example.co.uk", "indexed": False, "search_console": True},
    ]}


def test_a_search_console_problem_still_lists_indexed_clients(semantic):
    client, *_ = semantic
    index(client)
    class Broken:
        def properties(self):
            raise RuntimeError("signed out")
    app.dependency_overrides[get_search] = lambda: Broken()
    out = client.get("/clients").json()
    assert out["clients"] == [{"domain": DOMAIN, "indexed": True, "search_console": False}] and "signed out" in out["problems"][0]


def test_clients_needs_sign_in(semantic):
    client, *_ = semantic
    assert client.get("/clients", headers={"X-API-Key": "wrong"}).status_code == 401


def test_generate_without_a_sitemap_uses_the_index(semantic):
    client, set_model, *_ = semantic
    index(client)
    model = set_model({**FIRST_ANSWER, "internal_links": []})
    b = body(sitemap_urls=[])
    b["client"] = {"name": "Example Landscapes", "domain": DOMAIN}
    b["post"] = {**b["post"], "content": POST}
    r = client.post("/generate", json=b)
    assert r.status_code == 200, r.text
    listed = model.prompts[0].split("POST CONTENT")[0]
    assert BASE + "/p/101" in listed
    assert BASE + "/p/old" not in listed and BASE + "/missing" not in listed     # noindex and 404 pages never offered
    assert r.json()["stats"]["retrieval"] == "semantic"


def test_generate_without_a_sitemap_or_index_explains(semantic):
    client, set_model, *_ = semantic
    set_model(FIRST_ANSWER)
    b = body(sitemap_urls=[])
    b["client"] = {"name": "New Client", "domain": "new-client.co.uk"}
    r = client.post("/generate", json=b)
    assert r.status_code == 422 and r.json()["detail"]["code"] == "no_pages"


def test_index_accepts_a_shorter_budget(semantic, monkeypatch):
    client, *_ = semantic
    seen = {}
    import app.main as m
    real = m.build_index
    async def spy(*a, budget_s, **kw):
        seen["budget"] = budget_s
        return await real(*a, budget_s=budget_s, **kw)
    monkeypatch.setattr(m, "build_index", spy)
    r = client.post("/index", json={"client": {"name": "x", "domain": DOMAIN}, "sitemap_urls": [BASE + "/p/101"], "budget_s": 40})
    assert r.status_code == 200, r.text
    assert seen["budget"] == 40
    assert client.post("/index", json={"client": {"name": "x", "domain": DOMAIN}, "sitemap_urls": [BASE + "/p/101"],
                                       "budget_s": 5}).status_code == 422


def test_a_client_without_an_index_uses_their_sitemap_found_automatically(semantic, monkeypatch):
    client, set_model, *_ = semantic
    calls = []
    async def found(domain, transport=None):
        calls.append(domain)
        return BASE + "/sitemap.xml", [BASE + "/p/101", BASE + "/p/102"]
    monkeypatch.setattr(m, "find_sitemap", found)
    for _ in range(2):
        model = set_model({**FIRST_ANSWER, "internal_links": []})
        b = body(sitemap_urls=[])
        b["client"] = {"name": "RJ", "domain": "https://www.patios.example.co.uk/"}
        r = client.post("/generate", json=b)
        assert r.status_code == 200, r.text
        assert BASE + "/p/102" in model.prompts[0] and r.json()["stats"]["retrieval"] == "url"
    assert calls == ["patios.example.co.uk"]          # found once, then remembered


def test_index_finds_the_sitemap_itself(semantic, monkeypatch):
    client, *_ = semantic
    async def found(domain, transport=None):
        return BASE + "/sitemap.xml", SITEMAP
    monkeypatch.setattr(m, "find_sitemap", found)
    r = client.post("/index", json={"client": {"name": "x", "domain": DOMAIN}})
    assert r.status_code == 200, r.text
    assert r.json()["indexed"] == 4


# ---------- finding a sitemap ----------
def run_find(monkeypatch, routes):
    monkeypatch.setattr(crawl, "resolve", public_dns)
    def h(request):
        key = (request.url.host, request.url.path)
        return routes[key](request) if key in routes else httpx.Response(404, text="nope")
    return asyncio.run(find_sitemap("example-landscapes.co.uk", transport=httpx.MockTransport(h)))


def test_find_sitemap_falls_back_to_the_usual_addresses(monkeypatch):
    # robots.txt has no Sitemap line, and only the www host's /sitemap.xml exists (a sitemap index, as on the fake site)
    routes = {("www.example-landscapes.co.uk", p): handler for p in
              ("/robots.txt", "/sitemap.xml", "/sitemap-pages.xml", "/sitemap-more.xml.gz")}
    url, urls = run_find(monkeypatch, routes)
    assert url == BASE + "/sitemap.xml" and BASE + "/p/103" in urls


def test_find_sitemap_reads_robots_txt_first(monkeypatch):
    sm = '<urlset><url><loc>https://www.example-landscapes.co.uk/p/101</loc></url></urlset>'
    routes = {("www.example-landscapes.co.uk", "/robots.txt"):
                  lambda r: httpx.Response(200, text=ROBOTS + "Sitemap: https://www.example-landscapes.co.uk/custom-map.xml\n"),
              ("www.example-landscapes.co.uk", "/custom-map.xml"): lambda r: httpx.Response(200, text=sm),
              ("www.example-landscapes.co.uk", "/sitemap.xml"): handler}
    assert run_find(monkeypatch, routes) == (BASE + "/custom-map.xml", [BASE + "/p/101"])


def test_find_sitemap_ignores_sitemaps_on_other_sites(monkeypatch):
    routes = {("www.example-landscapes.co.uk", "/robots.txt"):
              lambda r: httpx.Response(200, text="Sitemap: https://evil.example.com/sitemap.xml\n")}
    with pytest.raises(Blocked):
        run_find(monkeypatch, routes)
