import asyncio
import time

import numpy as np
import pytest

from app import crawl, indexer
from app.embed import HashEmbedder, normalise
from app.indexer import build_index, summarise
from app.retrieve import chunk_post, rank_by_meaning, unlinkable
from app.store import CachedStore, Index, LocalStore
from fake_site import BASE, public_dns, transport

DOMAIN = "example-landscapes.co.uk"
SITEMAP = [BASE + p for p in ("/p/101", "/p/102", "/p/103", "/p/104", "/p/old", "/p/private", "/missing")]


@pytest.fixture(autouse=True)
def dns(monkeypatch):
    monkeypatch.setattr(crawl, "resolve", public_dns)


def build(store, embedder, sitemap=SITEMAP, **kw):
    return asyncio.run(build_index(DOMAIN, sitemap, store, embedder, transport=transport(), **kw))


@pytest.fixture
def store(tmp_path):
    return LocalStore(tmp_path)


# ---------- building the index ----------
def test_first_build_indexes_good_pages_and_explains_the_rest(store):
    stats = build(store, HashEmbedder())
    assert stats["indexed"] == 4 and stats["embedded_now"] == 4 and stats["remaining"] == 0
    assert stats["skipped"] == {"noindex": 1, "robots_txt": 1, "http_404": 1}
    idx = store.load(DOMAIN)
    assert idx.pages[BASE + "/p/102"]["title"] == "Patio Installation | Example Landscapes"
    assert idx.pages[BASE + "/p/102"]["summary"].startswith("Natural stone")
    assert set(idx.vectors) == {BASE + f"/p/{n}" for n in (101, 102, 103, 104)}


def test_second_build_does_nothing_until_pages_are_due_a_refresh(store):
    build(store, HashEmbedder())
    e = HashEmbedder()
    stats = build(store, e)
    assert stats["crawled_now"] == 0 and e.calls == 0


def test_refresh_only_re_embeds_pages_whose_text_changed(store, monkeypatch):
    build(store, HashEmbedder())
    idx = store.load(DOMAIN)
    for p in idx.pages.values():
        p["crawled_at"] -= 31 * 86400          # everything is now due a refresh
    idx.pages[BASE + "/p/103"]["hash"] = "changed"   # pretend this page's text has changed since
    store.save(idx)
    e = HashEmbedder()
    stats = build(store, e)
    assert stats["crawled_now"] == 7 and stats["unchanged"] == 3 and stats["embedded_now"] == 1
    assert e.texts and "Closeboard" in e.texts[0]


def test_pages_that_leave_the_sitemap_are_removed(store):
    build(store, HashEmbedder())
    stats = build(store, HashEmbedder(), sitemap=SITEMAP[:2])
    assert stats["removed"] == 5
    assert set(store.load(DOMAIN).pages) == set(SITEMAP[:2])


def test_new_pages_are_crawled_before_refreshes(store):
    idx = Index(DOMAIN, "hash-embedder", 256, pages={SITEMAP[0]: {"status": "indexed", "crawled_at": 0}})
    todo, drop = indexer.plan(idx, SITEMAP[:3], set(), now=time.time())
    assert todo == SITEMAP[1:3] + SITEMAP[:1] and drop == []


def test_running_out_of_time_leaves_the_rest_for_next_call(store, monkeypatch):
    stats = build(store, HashEmbedder(), budget_s=0)
    assert stats["crawled_now"] == 0 and stats["remaining"] == 7


def test_changing_embedding_model_starts_a_fresh_index(store):
    build(store, HashEmbedder(dims=256))
    e = HashEmbedder(dims=128)
    stats = build(store, e)
    assert stats["embedded_now"] == 4 and store.load(DOMAIN).dims == 128


def test_summary_of_an_index(store):
    assert summarise(None, "https://www.example-landscapes.co.uk")["exists"] is False
    build(store, HashEmbedder())
    s = summarise(store.load(DOMAIN), DOMAIN)
    assert s["indexed"] == 4 and s["skipped"] == 3 and s["skip_reasons"]["noindex"] == 1


# ---------- storage ----------
def test_index_round_trips_through_json():
    idx = Index(DOMAIN, "m", 4, pages={"u1": {"status": "indexed"}, "u2": {"status": "skipped"}},
                vectors={"u1": normalise(np.array([[1, 2, 3, 4]]))[0]}, updated_at=5)
    back = Index.from_json(idx.to_json())
    assert back.pages == idx.pages and np.allclose(back.vectors["u1"], idx.vectors["u1"]) and list(back.vectors) == ["u1"]


def test_cached_store_avoids_reloading(tmp_path):
    inner = LocalStore(tmp_path)
    loads = []
    real = inner.load
    inner.load = lambda d: loads.append(d) or real(d)
    cached = CachedStore(inner)
    cached.load(DOMAIN), cached.load(DOMAIN)
    assert loads == [DOMAIN]


# ---------- matching by meaning ----------
def test_chunks_follow_paragraphs_and_lead_with_title_and_keyword():
    plain = "\n".join(["para " * 100] * 6)
    chunks = chunk_post(plain, "My Title", "patios")
    assert chunks[0] == "My Title patios" and all(len(c) <= 2400 for c in chunks) and len(chunks) > 2


def test_long_posts_are_sampled_evenly():
    chunks = chunk_post("\n".join(f"section {i} " * 150 for i in range(40)))
    assert len(chunks) == 12 and chunks[0].startswith("section 0 ") and chunks[-1].startswith("section 39 ")


def test_pages_are_ranked_by_what_they_are_about_not_their_url(store):
    build(store, HashEmbedder())
    idx = store.load(DOMAIN)
    post = "Thinking about a new deck? Composite decking boards don't rot and need low maintenance. We fit joists and balustrades."
    ranked = rank_by_meaning(idx, SITEMAP, post, "", "", HashEmbedder())
    assert ranked[0][0] == BASE + "/p/104"           # /p/104 says nothing in its URL, only its content is about decking


def test_a_strong_match_in_one_section_counts(store):
    build(store, HashEmbedder())
    idx = store.load(DOMAIN)
    post = "\n".join(["General chat about the weather and the seasons in England. " * 25,
                      "Separately: closeboard fencing with concrete posts and gravel boards, plus garden gates."])
    ranked = dict(rank_by_meaning(idx, SITEMAP, post, "", "", HashEmbedder()))
    assert max(ranked, key=ranked.get) == BASE + "/p/103"


def test_noindex_and_dead_pages_are_never_link_candidates(store):
    build(store, HashEmbedder())
    idx = store.load(DOMAIN)
    assert unlinkable(idx, BASE + "/p/old") and unlinkable(idx, BASE + "/missing")
    assert not unlinkable(idx, BASE + "/p/private")   # blocked to us by robots.txt, but a real page for visitors
    assert not unlinkable(idx, BASE + "/p/101") and not unlinkable(None, BASE + "/p/101")


def test_score_is_the_best_section_not_the_average():
    # Page A matches one section perfectly; page B matches every other section a little.
    # A post with one paragraph on A's topic should still rank A first.
    eye = np.eye(4, dtype=np.float32)
    idx = Index(DOMAIN, "fixed", 4, pages={"A": {}, "B": {}},
                vectors={"A": eye[0], "B": normalise(np.array([[0, 1, 1, 1]]))[0]})

    class Fixed:
        name, dims = "fixed", 4

        def embed(self, texts, task):
            return eye[:len(texts)]          # chunk i points along axis i

    post = "\n".join(f"Section {i} " + "words " * 250 for i in range(4))   # four chunks
    ranked = rank_by_meaning(idx, ["A", "B"], post, "", "", Fixed())
    assert [u for u, _ in ranked] == ["A", "B"]


@pytest.mark.parametrize("raw, name", [
    ("my-project-post-desk-index", "my-project-post-desk-index"),
    ("gs://my-project-post-desk-index/", "my-project-post-desk-index"),
    ("  my-project-post-desk-index\n", "my-project-post-desk-index"),
    ("", ""),
])
def test_bucket_setting_is_tidied(raw, name):
    from app.store import bucket_name
    assert bucket_name(raw) == name


@pytest.mark.parametrize("raw", ["-post-desk-index", "Post_Desk!", "gs://-x-"])
def test_bad_bucket_setting_names_the_value(raw):
    from app.store import bucket_name
    with pytest.raises(ValueError, match="POST_DESK_BUCKET is"):
        bucket_name(raw)
