"""The generate step: rank the sitemap, ask the model, verify every link, retry the bad anchors once."""
from __future__ import annotations

import re
import time
import uuid

from . import core
from .crawl import normalise_domain
from .llm import FallbackModel, JSONModel, ModelError
from .prompts import generate_prompt, link_range, main_link_prompt, retry_prompt
from .gsc import SearchConsoleError, describe, page_key
from .retrieve import CONTACT, KIND_RANK, MAIN_SERVICE, page_kind, rank_by_meaning, unlinkable

CANDIDATE_LIMIT = 350       # URL-only ranking (no index): same as the page
SEMANTIC_LIMIT = 60         # pages chosen by meaning, sent with title and summary
FILL_LIMIT = 40             # plus not-yet-indexed pages ranked by URL, so new pages aren't missed
CONTACT_LIMIT = 3           # contact or quote pages always offered, so calls to action can be linked
MAIN_LIMIT = 12            # main service pages always offered, best match first, so they're never crowded out
CLOSE_LIMIT = 15            # pages close to page one in Google, brought in if they're relevant enough...
CLOSE_WITHIN = 200          # ...meaning within this many places of the top by meaning
SPARE_LINKS = 3             # semantic path: ask for a few more than the post can take, so the code picks the best by type
PRIORITY_RANK = {"high": 0, "medium": 1, "low": 2}


def _ask(model, prompt, **kw):
    """Calls the model, returning (answer, attempts). Models without attempt records report none."""
    if hasattr(model, "json_with_info"):
        return model.json_with_info(prompt, **kw)
    return model.json(prompt), []


def _ms(t0):
    return int((time.monotonic() - t0) * 1000)


def _clean_metas(raw) -> list[dict]:
    out = []
    for m in raw or []:
        if isinstance(m, dict) and isinstance(m.get("text"), str) and m["text"].strip():
            text = m["text"].strip()
            out.append({"text": text, "angle": str(m.get("angle") or ""), "length": len(text), "status": core.meta_status(len(text))})
    return out


def _priority(p) -> str:
    return p if p in ("high", "medium", "low") else "medium"


def _link_mix(links, pages, priority_keys) -> dict:
    """What kinds of page the kept links point to, so a run shows whether money pages got their share."""
    names = {"main service page": "main_service", "service or product page": "service", "contact or quote page": "contact",
             "article": "article"}
    mix = {"priority": 0, "main_service": 0, "service": 0, "contact": 0, "article": 0, "other": 0, "close_to_page_one": 0}
    for l in links:
        u = l["target_url"]
        p = (pages or {}).get(u)
        mix["priority" if core.norm_url(u) in priority_keys else names.get(page_kind(u, p), "other")] += 1
        mix["close_to_page_one"] += bool((p or {}).get("search", {}).get("close_to_page_one"))
    return mix


def _set_aside(suggested, kept_urls, verified_urls, known, pages, priority_keys) -> list[dict]:
    """Links the model suggested that didn't make it into the post, and why, so a run can be checked."""
    out, seen = [], set()
    for l in suggested:
        if not isinstance(l, dict) or not l.get("target_url"):
            continue
        u = core.norm_url(l["target_url"])
        if u in kept_urls or (u, l.get("anchor_text")) in seen:
            continue
        seen.add((u, l.get("anchor_text")))
        why = ("trimmed to fit the post's length" if u in verified_urls else
               "not in the sitemap" if u not in known else "anchor not found word for word in the post, or already used")
        kind = "priority page" if u in priority_keys else page_kind(l["target_url"], (pages or {}).get(l["target_url"]))
        out.append({"anchor_text": str(l.get("anchor_text") or ""), "target_url": l["target_url"], "type": kind, "why": why})
    return out[:20]


def _search_report(search, domain, log, run_id):
    if not (search and domain):
        return None, None
    try:
        return search.report(domain), None
    except SearchConsoleError as e:
        log(event="search_console_skipped", run_id=run_id, code=e.code, error=str(e))
        return None, f"{e.code}: {e}"
    except Exception as e:   # never fail a post over ranking data
        log(event="search_console_skipped", run_id=run_id, error=f"{type(e).__name__}: {e}")
        return None, f"{type(e).__name__}: {e}"


def _semantic(req, pool, store, embedder, log, run_id, report=None):
    """Candidates chosen by meaning from the client's page index, or None to fall back to URL ranking."""
    client, post = req["client"], req["post"]
    domain = normalise_domain(client.get("domain", ""))
    if not (store and embedder and domain):
        return None
    try:
        index = store.load(domain)
        if not index or not index.vectors:
            return None
        if index.model != embedder.name:
            log(event="index_model_mismatch", run_id=run_id, index_model=index.model, embedder=embedder.name)
            return None
        pool = [u for u in pool if not unlinkable(index, u)]
        plain, _ = core.post_plain(post["content"])
        ranked = rank_by_meaning(index, pool, plain, post.get("title", ""), post.get("keyword", ""), embedder)
        chosen = [u for u, _ in ranked[:SEMANTIC_LIMIT]]
        contact = [u for u, _ in ranked if page_kind(u, index.pages.get(u)) == CONTACT][:CONTACT_LIMIT]
        main = [u for u, _ in ranked if page_kind(u, index.pages.get(u)) == MAIN_SERVICE][:MAIN_LIMIT]
        close_keys = {page_key(o.url) for o in (report or {}).get("opportunities", [])}
        # Pages just off Google's first page gain most from a link, so relevant ones join the candidates.
        close = [u for u, _ in ranked[:CLOSE_WITHIN] if page_key(u) in close_keys][:CLOSE_LIMIT]
        unread = [u for u in pool if u not in index.vectors]
        fill = core.rank_urls(unread, post["content"], f'{post.get("keyword", "")} {post.get("title", "")}', FILL_LIMIT)
        picked = list(dict.fromkeys(chosen + main + contact + close))
        pages = {u: {**index.pages[u], "kind": page_kind(u, index.pages[u])} for u in picked}
        if report:
            for u, p in pages.items():
                st = report["pages"].get(page_key(u))
                if st:
                    p["search"] = describe(st, page_key(u) in close_keys)
        return {"ranked": picked + fill, "pages": pages, "index": index, "indexed": len(index.vectors),
                "close_offered": sum(1 for p in pages.values() if p.get("search", {}).get("close_to_page_one")),
                "top": [(u, round(s, 3)) for u, s in ranked[:5]]}
    except Exception as e:   # never fail a post because the index or embeddings are unavailable
        log(event="semantic_fallback", run_id=run_id, error=f"{type(e).__name__}: {e}")
        return None


def generate(req: dict, model: JSONModel, log=lambda **kw: None, store=None, embedder=None, search=None) -> dict:
    effort = (req.get("options") or {}).get("effort")
    t0 = time.monotonic()
    max_s = (req.get("options") or {}).get("max_seconds")
    left = (lambda: max_s - (time.monotonic() - t0)) if max_s else (lambda: 10_000)
    timed = hasattr(model, "json_with_info")
    run_id = uuid.uuid4().hex[:12]
    client, post = req["client"], req["post"]
    all_urls = [u.strip() for u in req["sitemap_urls"] if isinstance(u, str) and re.match(r"https?://", u.strip(), re.I)]

    self_url = core.norm_url(post.get("url"))
    pool = [u for u in all_urls if core.norm_url(u) != self_url]
    priority = [str(u) for u in client.get("priority_pages") or []]
    timings = {}
    t = time.monotonic()
    report, search_error = _search_report(search, normalise_domain(client.get("domain", "")), log, run_id) if store and embedder else (None, None)
    sem = _semantic(req, pool, store, embedder, log, run_id, report)
    timings["retrieval_ms"] = _ms(t)
    if sem:
        ranked, pages = sem["ranked"], sem["pages"]
        for u in priority:   # priority pages get their title and summary too, if indexed
            p = sem["index"].pages.get(u)
            if u not in pages and p and p.get("status") == "indexed":
                pages[u] = {**p, "kind": page_kind(u, p)}
    else:
        ranked, pages = core.rank_urls(pool, post["content"], f'{post.get("keyword", "")} {post.get("title", "")}', CANDIDATE_LIMIT), None
    candidates = list(dict.fromkeys(priority + ranked))

    plain, headings = core.post_plain(post["content"])
    lo, hi = link_range(len(plain.split()))
    t = time.monotonic()
    ask = (lo, min(hi + SPARE_LINKS, 13)) if pages is not None else (lo, hi)
    # With a time limit: one try per model. The main model gets most of the time left; the fallback gets what remains.
    budget = {}
    if max_s and timed:
        main_s = min(45, max(10, left() * 0.65))
        budget = {"attempts": 1, "timeout": main_s}
        if isinstance(model, FallbackModel):
            budget["fallback_timeout"] = min(60, max(8, left() - main_s - 2))
    try:
        out, attempts = _ask(model, generate_prompt(client, post, candidates, len(all_urls), priority, pages, links=ask),
                             **({"effort": effort} if effort else {}), **budget)
    except ModelError as e:
        e.timings = {**timings, "model_ms": _ms(t)}      # so the error can say where the time went
        raise
    timings["model_ms"] = _ms(t)

    known = {core.norm_url(u) for u in all_urls}
    known.discard(self_url)
    taken = {"urls": set(), "anchors": set()}
    suggested = out.get("internal_links") or []
    every_suggestion = list(suggested)      # first pass plus the anchor fix-up, for set_aside
    links, bad = core.check_links(suggested, plain, known, taken, headings)
    retried = len(bad)
    if bad and left() < 8:
        log(event="retry_skipped", run_id=run_id, reason="out of time")
    elif bad:
        t = time.monotonic()
        try:
            # One attempt with a shorter cut-off: it only fixes anchors, and the first pass already stands.
            fixed, more = _ask(model, retry_prompt(bad, sorted(taken["anchors"]), plain),
                               **({"attempts": 1, "timeout": min(60, max(5, left() - 3)), "effort": "low"} if timed else {}))
            attempts += more
            every_suggestion += [l for l in fixed.get("internal_links") or [] if isinstance(l, dict)]
            links += core.check_links(fixed.get("internal_links") or [], plain, known, taken, headings)[0]
        except Exception as e:  # the first pass still stands if the retry fails
            log(event="retry_failed", run_id=run_id, error=str(e))
        timings["anchor_fix_ms"] = _ms(t)

    priority_keys = {core.norm_url(u) for u in priority}
    tier = lambda u: 0 if core.norm_url(u) in priority_keys else KIND_RANK.get(page_kind(u, (pages or {}).get(u)), 2)

    # No priority or main service link yet? Ask once, briefly, for one. The code below then keeps it.
    main_link = None
    targets = [(u, pages[u]) for u in dict.fromkeys(priority + candidates)
               if pages is not None and u in pages and tier(u) <= 1 and core.norm_url(u) in known][:MAIN_LIMIT]
    if targets and not any(tier(l["target_url"]) <= 1 for l in links) and left() < 8:
        main_link = "skipped: not enough time left"
    elif targets and not any(tier(l["target_url"]) <= 1 for l in links):
        t = time.monotonic()
        try:
            found, more = _ask(model, main_link_prompt(targets, sorted(taken["anchors"]), plain),
                               **({"attempts": 1, "timeout": min(45, max(5, left() - 3)), "effort": "low"} if timed else {}))
            attempts += more
            offered = [l for l in found.get("internal_links") or [] if isinstance(l, dict)
                       and core.norm_url(l.get("target_url")) in {core.norm_url(u) for u, _ in targets}][:1]
            every_suggestion += offered
            got = core.check_links(offered, plain, known, taken, headings)[0]
            links += [{**l, "priority": "high"} for l in got]
            main_link = "added" if got else ("anchor not in the post" if offered else "nothing in the post fits")
        except Exception as e:   # the links already found still stand
            main_link = "failed"
            log(event="main_link_failed", run_id=run_id, error=str(e))
        timings["main_link_ms"] = _ms(t)

    # Keep to the link range for the post's length. The model's priority comes first; between equals, the client's
    # priority pages, then main service pages, other service pages, articles; then pages close to page one; then post order.
    # On the semantic path the model offers a few spares, so this is where the final choice is made.
    folded = core.fold(plain)
    close = lambda u: (pages or {}).get(u, {}).get("search", {}).get("close_to_page_one", False)
    verified = len(links)
    verified_urls = {core.norm_url(l["target_url"]) for l in links}
    trimmed = 0
    kept_urls = verified_urls
    if len(links) > hi:
        links.sort(key=lambda l: (PRIORITY_RANK.get(l.get("priority"), 1), tier(l["target_url"]), not close(l["target_url"]),
                                  folded.find(core.fold(l["anchor_text"]))))
        keep, rest = links[:hi], links[hi:]
        # At least one priority or main service page whenever the model found a usable one: it takes the last place.
        best = next((l for l in rest if tier(l["target_url"]) <= 1), None)
        if best and not any(tier(l["target_url"]) <= 1 for l in keep):
            keep[-1] = best
        trimmed, links = len(links) - hi, keep
    kept_urls = {core.norm_url(l["target_url"]) for l in links}
    links.sort(key=lambda l: folded.find(core.fold(l["anchor_text"])))
    links = [{"anchor_text": l["anchor_text"], "sentence": l["sentence"], "target_url": l["target_url"],
              "reason": str(l.get("reason") or ""), "priority": _priority(l.get("priority")), "verified": True} for l in links]

    title_tag = str(out.get("title_tag") or "")
    result = {
        "run_id": run_id,
        "title_tag": title_tag,
        "title_status": core.title_status(len(title_tag)),
        "primary_keyword": post.get("keyword") or str(out.get("primary_keyword") or ""),
        "meta_descriptions": _clean_metas(out.get("meta_descriptions")),
        "internal_links": links,
        "link_back_from": [{"source_url": b["source_url"], "anchor_text": str(b.get("anchor_text") or ""), "reason": str(b.get("reason") or "")}
                           for b in out.get("link_back_from") or [] if isinstance(b, dict) and core.norm_url(b.get("source_url")) in known],
        "headings": [{"text": h["text"], "level": min(6, max(1, int(h.get("level") or 2)))}
                     for h in out.get("headings") or [] if isinstance(h, dict) and h.get("text")],
        "lists": [{"type": "ol" if l.get("type") == "ol" else "ul", "items": [str(i) for i in l["items"]]}
                  for l in out.get("lists") or [] if isinstance(l, dict) and isinstance(l.get("items"), list)],
        "stats": {"candidates": len(candidates), "suggested": len(suggested), "verified": verified,
                  "dropped": max(0, len(suggested) - verified), "retried": retried, "model": model.name,
                  "retrieval": "semantic" if sem else "url", "indexed_pages": sem["indexed"] if sem else 0,
                  "search_console": ({"property": report["property"], "close_to_page_one_offered": sem["close_offered"] if sem else 0,
                                      "close_to_page_one_linked": sum(1 for l in links if (pages or {}).get(l["target_url"], {}).get("search", {}).get("close_to_page_one"))}
                                     if report else ({"error": search_error} if search_error else None)),
                  "link_mix": _link_mix(links, pages, priority_keys),
                  "main_link_followup": main_link,
                  "set_aside": _set_aside(every_suggestion, kept_urls, verified_urls, known, pages, priority_keys),
                  "provider": getattr(model, "provider", None), "fell_back": getattr(model, "fell_back", None), "effort": effort or getattr(model, "effort", None), "link_range": [lo, hi], "trimmed": trimmed, "timings": timings, "model_attempts": attempts,
                  "ms": int((time.monotonic() - t0) * 1000)},
    }
    log(event="generate", run_id=run_id, client=client["name"], top_matches=sem["top"] if sem else None, **result["stats"])
    return result
