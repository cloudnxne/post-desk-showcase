"""Crawling a site whose firewall cuts us off after a burst, as lawnco.example.co.uk did on the first real run:
the first few pages worked, then every connection timed out."""
import asyncio

import httpx
import pytest

from app import crawl, indexer
from app.crawl import BACKOFF_AFTER
from app.embed import HashEmbedder
from app.indexer import build_index
from app.store import LocalStore
from fake_site import BASE, handler, public_dns

DOMAIN = "example-landscapes.co.uk"
URLS = [BASE + f"/p/{n}" for n in (101, 102, 103, 104)] + [BASE + f"/extra/{i}" for i in range(20)]


@pytest.fixture(autouse=True)
def dns(monkeypatch):
    monkeypatch.setattr(crawl, "resolve", public_dns)


def firewall(allow: int, state: dict | None = None):
    """Serves `allow` page requests, then every connection times out (until state['open'] is set)."""
    state = state if state is not None else {}
    state.setdefault("served", 0)

    def h(request):
        if request.url.path == "/robots.txt":
            return handler(request)
        if not state.get("open") and state["served"] >= allow:
            raise httpx.ConnectTimeout("timed out", request=request)
        state["served"] += 1
        if request.url.path.startswith("/extra/"):
            return httpx.Response(200, text=f"<html><title>Extra {request.url.path}</title><main>{'Lawn care tips. ' * 30}</main></html>",
                                  headers={"content-type": "text/html"})
        return handler(request)
    return httpx.MockTransport(h), state


def test_backs_off_when_the_site_stops_answering():
    t, _ = firewall(allow=3)
    run = asyncio.run(crawl.crawl(URLS, DOMAIN, concurrency=1, transport=t))
    ok = [r for r in run.results if r.ok]
    failed = [r for r in run.results if not r.ok]
    assert len(ok) == 3
    assert len(failed) == BACKOFF_AFTER and all(r.reason == "timeout" for r in failed)
    assert "stopped answering" in run.stopped_early
    assert len(run.results) < len(URLS)          # it stopped rather than trying all 24


def test_one_failure_isnt_a_block():
    calls = {"n": 0}

    def h(request):
        if request.url.path == "/p/102":
            calls["n"] += 1
            if calls["n"] == 1:
                raise httpx.ReadTimeout("slow", request=request)   # first try times out, the retry works
        return handler(request)
    run = asyncio.run(crawl.crawl(URLS[:4], DOMAIN, concurrency=1, transport=httpx.MockTransport(h)))
    assert all(r.ok for r in run.results) and run.stopped_early == ""


def test_failure_reasons_are_specific():
    def h(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if request.url.path == "/p/101":
            raise httpx.ConnectError("Connection refused", request=request)
        if request.url.path == "/p/102":
            return httpx.Response(429, headers={"content-type": "text/html"})
        return handler(request)
    run = asyncio.run(crawl.crawl(URLS[:3], DOMAIN, concurrency=1, transport=httpx.MockTransport(h)))
    got = {r.url.rsplit("/", 1)[1]: (r.reason, r.error) for r in run.results}
    assert got["101"] == ("connect_failed", "Connection refused")
    assert got["102"][0] == "http_429" and got["103"][0] == ""


def test_requests_are_spaced_out(monkeypatch):
    slept = []
    real_sleep = asyncio.sleep

    async def record(s, *a):
        slept.append(s)
        await real_sleep(0)
    monkeypatch.setattr(crawl.asyncio, "sleep", record)
    t, _ = firewall(allow=100)
    asyncio.run(crawl.crawl(URLS[:4], DOMAIN, concurrency=4, delay=0.5, transport=t))
    assert len([s for s in slept if s > 0.3]) >= 3      # four requests, at least three waits between them


def test_robots_crawl_delay_is_honoured(monkeypatch):
    slept = []
    real_sleep = asyncio.sleep

    async def record(s, *a):
        slept.append(s)
        await real_sleep(0)
    monkeypatch.setattr(crawl.asyncio, "sleep", record)

    def h(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nCrawl-delay: 3\n", headers={"content-type": "text/plain"})
        return handler(request)
    asyncio.run(crawl.crawl(URLS[:3], DOMAIN, concurrency=3, delay=0, transport=httpx.MockTransport(h)))
    assert max(slept) > 2.5


def test_defaults_are_gentle(monkeypatch):
    monkeypatch.delenv("POST_DESK_CRAWL_DELAY_S", raising=False)
    seen = {}
    real = crawl._Pacer.__init__

    def spy(self, gap):
        seen["gap"] = gap
        real(self, gap)
    monkeypatch.setattr(crawl._Pacer, "__init__", spy)
    sem = []
    real_sem = asyncio.Semaphore
    monkeypatch.setattr(crawl.asyncio, "Semaphore", lambda n: sem.append(n) or real_sem(n))
    asyncio.run(crawl.crawl([], DOMAIN))
    assert seen["gap"] >= 0.5 and sem == [2]


# ---------- indexing across runs ----------
def build(store, t):
    return asyncio.run(build_index(DOMAIN, URLS, store, HashEmbedder(), transport=t))


def test_blocked_pages_are_retried_on_the_next_run(tmp_path):
    store = LocalStore(tmp_path)
    t, state = firewall(allow=3)
    first = build(store, t)
    # Indexing fetches 2 at a time, so one more request can already be in flight when it pauses.
    assert first["indexed"] == 3 and BACKOFF_AFTER <= first["retry_next_run"] <= BACKOFF_AFTER + 1
    assert first["remaining"] == len(URLS) - 3 and first["stopped_early"]
    assert first["skip_examples"]["timeout"][0].endswith("(ConnectTimeout)")

    state["open"] = True                                   # the firewall lets us back in
    second = build(store, t)
    assert second["indexed"] == len(URLS) and second["remaining"] == 0 and not second["stopped_early"]


def test_a_failed_refresh_keeps_the_page_we_already_had(tmp_path):
    store = LocalStore(tmp_path)
    t, state = firewall(allow=100)
    build(store, t)
    idx = store.load(DOMAIN)
    for p in idx.pages.values():
        p["crawled_at"] -= 31 * 86400
    store.save(idx)

    t2, _ = firewall(allow=0)                              # every refresh times out
    stats = build(store, t2)
    idx = store.load(DOMAIN)
    assert stats["indexed"] == len(URLS)                   # nothing lost
    assert all(u in idx.vectors for u in URLS)
    assert BACKOFF_AFTER <= sum(1 for p in idx.pages.values() if p.get("transient")) <= BACKOFF_AFTER + 1


def test_temporary_failures_stay_linkable(tmp_path):
    from app.retrieve import unlinkable
    store = LocalStore(tmp_path)
    t, _ = firewall(allow=0)
    build(store, t)
    idx = store.load(DOMAIN)
    assert not any(unlinkable(idx, u) for u in URLS)


def test_plan_puts_retries_after_new_pages():
    from app.store import Index
    idx = Index(DOMAIN, "m", 4, pages={"a": {"status": "skipped", "reason": "timeout", "transient": True, "crawled_at": 9e12},
                                       "b": {"status": "indexed", "crawled_at": 9e12}})
    todo, _ = indexer.plan(idx, ["a", "b", "c"], set(), now=9e12)
    assert todo == ["c", "a"]


def test_old_style_fetch_failures_are_retried_too():
    """Indexes saved before this change marked 144 lawnco.example.co.uk pages 'fetch_failed' with no retry marker."""
    from app.store import Index
    idx = Index(DOMAIN, "m", 4, pages={"a": {"status": "skipped", "reason": "fetch_failed", "crawled_at": 9e12},
                                       "b": {"status": "skipped", "reason": "noindex", "crawled_at": 9e12}})
    todo, _ = indexer.plan(idx, ["a", "b"], set(), now=9e12)
    assert todo == ["a"]
