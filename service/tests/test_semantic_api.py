"""The API with a page index: /index builds it, /generate uses it, and everything degrades to URL ranking when it can't."""
import pytest

from app import crawl, indexer
from app.embed import EmbedError, HashEmbedder
from app.main import app, get_embedder, get_store
from app.store import LocalStore
from conftest import FIRST_ANSWER, RETRY_ANSWER, body
from fake_site import BASE, public_dns, transport

DOMAIN = "example-landscapes.co.uk"
SITEMAP = [BASE + p for p in ("/p/101", "/p/102", "/p/103", "/p/104", "/p/old", "/missing")]
POST = ("Our garden design service maps out every bed before a spade goes in.\n\n"
        "For the deck we fit composite decking boards on joists, low maintenance and no rot.\n\n"
        "We also handle patio installation with natural stone slabs.")


@pytest.fixture
def semantic(api, tmp_path, monkeypatch):
    """The API with a local index store, the hash embedder, and the fake website."""
    monkeypatch.setattr(crawl, "resolve", public_dns)
    real_build = indexer.build_index
    monkeypatch.setattr("app.main.build_index", lambda *a, **kw: real_build(*a, transport=transport(), **kw))
    real_fetch = crawl.fetch_sitemap
    monkeypatch.setattr("app.main.fetch_sitemap", lambda url, domain: real_fetch(url, domain, transport=transport()))
    store, embedder = LocalStore(tmp_path), HashEmbedder()
    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[get_embedder] = lambda: embedder
    client, set_model = api
    return client, set_model, store, embedder


def index(client):
    return client.post("/index", json={"client": {"name": "Example Landscapes", "domain": "www.example-landscapes.co.uk"},
                                       "sitemap_urls": SITEMAP})


def generate(client, set_model, **post):
    model = set_model({**FIRST_ANSWER, "internal_links": []})
    b = body(sitemap_urls=SITEMAP)
    b["client"]["priority_pages"] = []
    b["post"] = {**b["post"], "content": POST, **post}
    r = client.post("/generate", json=b)
    assert r.status_code == 200, r.text
    return r.json(), model.prompts[0]


# ---------- /index ----------
def test_index_endpoint_builds_the_index(semantic):
    client, *_ = semantic
    r = index(client)
    assert r.status_code == 200, r.text
    s = r.json()
    assert s["domain"] == DOMAIN and s["indexed"] == 4 and s["remaining"] == 0
    assert s["skipped"] == {"noindex": 1, "http_404": 1}


def test_index_status(semantic):
    client, *_ = semantic
    assert client.get(f"/index/{DOMAIN}").json()["exists"] is False
    index(client)
    st = client.get("/index/https://www.example-landscapes.co.uk").json()
    assert st["exists"] and st["indexed"] == 4


def test_index_needs_a_domain(semantic):
    client, *_ = semantic
    r = client.post("/index", json={"client": {"name": "No Domain"}, "sitemap_urls": SITEMAP})
    assert r.status_code == 422


def test_index_needs_sign_in(semantic):
    client, *_ = semantic
    r = client.post("/index", json={"client": {"name": "x", "domain": DOMAIN}, "sitemap_urls": SITEMAP}, headers={"X-API-Key": "wrong"})
    assert r.status_code == 401


def test_index_reports_embedding_failures_clearly(semantic, monkeypatch):
    client, _, _, embedder = semantic
    monkeypatch.setattr(embedder, "embed", lambda *a: (_ for _ in ()).throw(EmbedError("Vertex AI embedding failed (403): permission denied")))
    r = index(client)
    assert r.status_code == 502 and "403" in r.json()["detail"]["message"]


def test_index_is_off_when_not_configured(api):
    client, _ = api
    app.dependency_overrides[get_store] = lambda: None
    app.dependency_overrides[get_embedder] = lambda: None
    assert index(client).status_code == 503


# ---------- /generate with an index ----------
def test_generate_uses_the_index_and_shows_claude_page_summaries(semantic):
    client, set_model, *_ = semantic
    index(client)
    out, prompt = generate(client, set_model)
    assert out["stats"]["retrieval"] == "semantic" and out["stats"]["indexed_pages"] == 4
    assert "EXISTING SITE PAGES" in prompt
    assert "Title: Composite Decking | Example Landscapes" in prompt
    assert "About: Low maintenance composite decking" in prompt
    assert "Judge relevance from each page's title and summary" in prompt


def test_closest_pages_come_first(semantic):
    client, set_model, *_ = semantic
    index(client)
    _, prompt = generate(client, set_model, content="Composite decking boards, joists and balustrades for a low maintenance deck.")
    listed = [l for l in prompt.split("EXISTING SITE PAGES")[1].split("POST CONTENT")[0].splitlines() if l.startswith("https://")]
    assert listed[0] == BASE + "/p/104"


def test_noindex_and_dead_pages_are_left_out(semantic):
    client, set_model, *_ = semantic
    index(client)
    _, prompt = generate(client, set_model)
    section = prompt.split("EXISTING SITE PAGES")[1].split("POST CONTENT")[0]
    assert BASE + "/p/old" not in section and BASE + "/missing" not in section


def test_without_an_index_it_works_exactly_as_before(semantic):
    client, set_model, *_ = semantic
    out, prompt = generate(client, set_model)
    assert out["stats"]["retrieval"] == "url"
    assert "EXISTING SITE URLS" in prompt and "Judge relevance from the URL path." in prompt


def test_embedding_trouble_falls_back_to_url_ranking(semantic, monkeypatch, caplog):
    client, set_model, _, embedder = semantic
    index(client)
    monkeypatch.setattr(embedder, "embed", lambda *a: (_ for _ in ()).throw(EmbedError("quota")))
    out, prompt = generate(client, set_model)
    assert out["stats"]["retrieval"] == "url" and "EXISTING SITE URLS" in prompt
    assert "semantic_fallback" in caplog.text


def test_index_from_another_embedding_model_is_not_mixed_in(semantic):
    client, set_model, store, _ = semantic
    index(client)
    other = HashEmbedder()
    other.name = "other-model"
    app.dependency_overrides[get_embedder] = lambda: other
    out, _ = generate(client, set_model)
    assert out["stats"]["retrieval"] == "url"


def test_pages_not_yet_indexed_are_still_offered(semantic):
    client, set_model, *_ = semantic
    index(client)
    new_page = "https://www.example-landscapes.co.uk/services/new-patio-offer/"
    model = set_model({**FIRST_ANSWER, "internal_links": []})
    b = body(sitemap_urls=SITEMAP + [new_page])
    b["client"]["priority_pages"] = []
    b["post"]["content"] = POST
    client.post("/generate", json=b)
    assert new_page in model.prompts[0]


def test_index_from_a_sitemap_url(semantic):
    client, *_ = semantic
    r = client.post("/index", json={"client": {"name": "x", "domain": DOMAIN}, "sitemap_url": BASE + "/sitemap.xml"})
    assert r.status_code == 200, r.text
    assert r.json()["pages_in_sitemap"] == 4 and r.json()["indexed"] == 4


def test_index_needs_some_urls(semantic):    # none given and no sitemap could be found
    client, *_ = semantic
    assert client.post("/index", json={"client": {"name": "x", "domain": DOMAIN}}).status_code == 422


def test_unreadable_sitemap_is_explained(semantic):
    client, *_ = semantic
    r = client.post("/index", json={"client": {"name": "x", "domain": DOMAIN}, "sitemap_url": "https://competitor.com/sitemap.xml"})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "sitemap_unreadable"


def test_a_broken_store_never_stops_generate(api, monkeypatch, caplog):
    """If Cloud Storage can't be reached (bad bucket, no permission), posts still work with URL ranking."""
    from app.store import GCSStore

    class Broken:
        def bucket(self, name):
            raise PermissionError("403 storage.objects.get denied")

    client, set_model = api
    app.dependency_overrides[get_store] = lambda: GCSStore("post-desk-index", client=Broken())
    app.dependency_overrides[get_embedder] = lambda: HashEmbedder()
    set_model({**FIRST_ANSWER, "internal_links": []})
    b = body(sitemap_urls=SITEMAP)
    b["client"]["domain"] = DOMAIN
    r = client.post("/generate", json=b)
    assert r.status_code == 200 and r.json()["stats"]["retrieval"] == "url"
    assert "semantic_fallback" in caplog.text
    assert client.get(f"/index/{DOMAIN}").status_code == 502


def test_store_set_up_errors_are_contained(monkeypatch):
    from app import main
    main.get_store.cache_clear()
    monkeypatch.setattr(main, "default_store", lambda: (_ for _ in ()).throw(RuntimeError("bad config")))
    assert main.get_store() is None
    main.get_store.cache_clear()
