"""Builds and refreshes a client's page index from its sitemap.

Each call does as much as it can within a time budget (Cloud Run requests are capped at 5 minutes) and
reports how many pages are left, so the caller just calls again until `remaining` is 0. Pages already in
the index are only re-crawled after REFRESH_DAYS, and only re-embedded if their text actually changed.
"""
from __future__ import annotations

import asyncio
import hashlib
import time
from collections import Counter

from .crawl import TRANSIENT, allowed_hosts, crawl, embed_text, normalise_domain, summary
from .embed import DOC, Embedder
from .store import Index, IndexStore

REFRESH_DAYS = 30
DEFAULT_BUDGET_S = 200


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def plan(index: Index, sitemap: list[str], hosts: set[str], now: float) -> tuple[list[str], list[str]]:
    """Which URLs to crawl (new first, then ones that failed temporarily last time, then the oldest due a
    refresh), and which to drop from the index."""
    wanted = list(dict.fromkeys(u.strip() for u in sitemap if isinstance(u, str) and u.strip()))
    wanted_set = set(wanted)
    drop = [u for u in index.pages if u not in wanted_set]
    new = [u for u in wanted if u not in index.pages]
    # Temporary failures (also from indexes saved before the "transient" marker existed) are retried every run.
    retry = [u for u in wanted if index.pages.get(u, {}).get("transient")
             or (index.pages.get(u, {}).get("status") == "skipped" and index.pages[u].get("reason") in TRANSIENT)]
    stale = sorted((u for u in wanted if u in index.pages and u not in retry
                    and now - index.pages[u].get("crawled_at", 0) > REFRESH_DAYS * 86400),
                   key=lambda u: index.pages[u].get("crawled_at", 0))
    return new + retry + stale, drop


async def build_index(domain: str, sitemap: list[str], store: IndexStore, embedder: Embedder,
                      budget_s: float = DEFAULT_BUDGET_S, transport=None, log=lambda **kw: None) -> dict:
    t0 = time.monotonic()
    domain = normalise_domain(domain)
    index = store.load(domain)
    if index is None or index.model != embedder.name or index.dims != embedder.dims:
        # A different embedding model's vectors can't be compared with new ones, so start afresh.
        index = Index(domain=domain, model=embedder.name, dims=embedder.dims)

    now = time.time()
    todo, drop = plan(index, sitemap, allowed_hosts(domain), now)
    for u in drop:
        index.pages.pop(u, None)
        index.vectors.pop(u, None)

    # Leave time at the end for embedding and saving.
    loop = asyncio.get_running_loop()
    deadline = loop.time() + budget_s * 0.6
    run = await crawl(todo, domain, deadline=deadline, transport=transport)
    results = run.results

    reasons: Counter = Counter()
    examples: dict[str, list[str]] = {}
    retry_next = 0
    to_embed: list[tuple[str, dict, str]] = []
    unchanged = 0
    for r in results:
        if not r.ok:
            reasons[r.reason] += 1
            if len(examples.setdefault(r.reason, [])) < 3:
                examples[r.reason].append(r.url + (f" ({r.error})" if r.error else ""))
            if r.reason in TRANSIENT:
                retry_next += 1
                old = index.pages.get(r.url)
                if old and old.get("status") == "indexed":
                    old["transient"] = True          # keep what we had; try again next run
                else:
                    index.pages[r.url] = {"status": "skipped", "reason": r.reason, "transient": True, "crawled_at": now}
                continue
            index.pages[r.url] = {"status": "skipped", "reason": r.reason, "crawled_at": now}
            index.vectors.pop(r.url, None)
            continue
        text = embed_text(r.page)
        h = _hash(text)
        record = {"status": "indexed", "title": r.page["title"], "h1": r.page["h1"], "meta": r.page["meta"],
                  "summary": summary(r.page), "hash": h, "crawled_at": now}
        old = index.pages.get(r.url)
        if old and old.get("hash") == h and r.url in index.vectors:
            unchanged += 1
            index.pages[r.url] = record
        else:
            to_embed.append((r.url, record, text))

    if to_embed:
        vectors = await asyncio.to_thread(embedder.embed, [t for _, _, t in to_embed], DOC)
        for (url, record, _), vec in zip(to_embed, vectors):
            index.pages[url] = record
            index.vectors[url] = vec

    index.updated_at = time.time()
    if results or drop:
        store.save(index)

    crawled = {r.url for r in results}
    stats = {
        "domain": domain,
        "pages_in_sitemap": len(set(sitemap)),
        "indexed": sum(1 for p in index.pages.values() if p.get("status") == "indexed"),
        "crawled_now": len(results),
        "embedded_now": len(to_embed),
        "unchanged": unchanged,
        "removed": len(drop),
        "skipped": dict(reasons),
        "skip_examples": examples,
        "retry_next_run": retry_next,
        "remaining": len([u for u in todo if u not in crawled]) + retry_next,
        "stopped_early": run.stopped_early,
        "model": embedder.name,
        "seconds": round(time.monotonic() - t0, 1),
    }
    log(event="index", **stats)
    return stats


def summarise(index: Index | None, domain: str) -> dict:
    if index is None:
        return {"domain": normalise_domain(domain), "exists": False}
    statuses = Counter(p.get("status") for p in index.pages.values())
    return {"domain": index.domain, "exists": True, "model": index.model, "dims": index.dims,
            "indexed": statuses.get("indexed", 0), "skipped": statuses.get("skipped", 0),
            "updated_at": index.updated_at,
            "skip_reasons": dict(Counter(p.get("reason") for p in index.pages.values() if p.get("status") == "skipped"))}
