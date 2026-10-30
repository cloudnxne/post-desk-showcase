"""Post Desk API. Run locally with: uvicorn app.main:app --reload (from the service folder)."""
from __future__ import annotations

import asyncio
import json
import logging
import time

import httpx
import os
import sys
from functools import lru_cache

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from . import copy_page
from fastapi.middleware.cors import CORSMiddleware

from .auth import Caller, require_caller
from .crawl import Blocked, fetch_sitemap, find_sitemap, normalise_domain
from .embed import EmbedError, Embedder, VertexEmbedder
from .generate import generate
from .gsc import PersonFirst, SearchConsole, SearchConsoleError, describe, person_console
from .indexer import build_index, summarise
from .llm import FallbackModel, JSONModel, ModelError, default_model, make_model
from .schemas import GenerateRequest, GenerateResponse, IndexRequest, IndexStats
from .retrieve import unlinkable
from .store import IndexStore, default_store

VERSION = "0.14.1"

logging.basicConfig(level=logging.INFO, stream=sys.stdout, format="%(message)s")
_log = logging.getLogger("post-desk")


def log(**fields):
    """One JSON line per event; Cloud Run sends stdout to Cloud Logging, which reads the severity and fields."""
    _log.info(json.dumps({"severity": "ERROR" if "error" in fields else "INFO", **fields}))


app = FastAPI(
    title="Post Desk API",
    version=VERSION,
    description="Meta descriptions, verified internal links, headings and lists for a client's new blog post. "
                "Every link is checked against the client's sitemap and every anchor against the post itself.",
)

origins = [o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "").split(",") if o.strip()]
if origins:
    app.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=["POST", "GET"], allow_headers=["X-API-Key", "Authorization", "Content-Type"])


@lru_cache
def get_model() -> JSONModel:
    return default_model()


@lru_cache
def model_for(provider: str) -> JSONModel:
    """A client for a provider asked for in the request, made once and reused."""
    return make_model(provider)


def fallback_model() -> JSONModel | None:
    p = os.environ.get("POST_DESK_FALLBACK_PROVIDER", "gemini").strip().lower()
    if p in ("", "none", "off"):
        return None
    try:
        return model_for(p)
    except Exception as e:
        log(event="fallback_unavailable", provider=p, error=f"{type(e).__name__}: {e}")
        return None


def pick_model(req: GenerateRequest, default: JSONModel = Depends(get_model), caller: Caller = Depends(require_caller)) -> JSONModel:
    p = req.options.provider
    if not p:
        fb = fallback_model()
        if fb is None or getattr(fb, "provider", None) == getattr(default, "provider", None):
            return default
        # A fresh wrapper per request: it remembers, for this run only, whether the main model has failed.
        return FallbackModel(default, fb, force_failure=req.options.force_fallback and caller.kind == "api_key")
    if p == getattr(default, "provider", None):
        return default
    try:
        return model_for(p)
    except Exception as e:   # e.g. no Anthropic key, or no Google project configured
        raise HTTPException(400, {"code": "provider_unavailable", "message": f"The {p} model isn't available on this service ({e})."})


_store_problem = ""


@lru_cache
def get_store() -> IndexStore | None:
    global _store_problem
    try:
        return default_store()
    except Exception as e:   # a broken store must never take /generate down with it
        _store_problem = f"{type(e).__name__}: {e}"
        log(event="store_unavailable", error=_store_problem)
        return None


@lru_cache
def get_embedder() -> Embedder | None:
    """Vertex AI embeddings when a project is configured; without one, posts use URL ranking as before."""
    if not os.environ.get("GOOGLE_CLOUD_PROJECT"):
        return None
    try:
        return VertexEmbedder()
    except Exception as e:
        log(event="embedder_unavailable", error=f"{type(e).__name__}: {e}")
        return None


_search_problem = ""


@lru_cache
def get_search() -> SearchConsole | None:
    """Search Console ranking data: a connected account if one is saved, otherwise the service account.
    POST_DESK_GSC=off switches it off."""
    global _search_problem
    if os.environ.get("POST_DESK_GSC", "on").strip().lower() in ("off", "none", "0", "false"):
        return None
    try:
        return SearchConsole()
    except Exception as e:
        _search_problem = f"{type(e).__name__}: {e}"
        log(event="search_console_unavailable", error=_search_problem)
        return None


def get_user_search(request: Request, caller: Caller = Depends(require_caller),
                    shared: SearchConsole | None = Depends(get_search)):
    """The signed-in person's own Search Console when the Google Doc sends their access token, falling back to the
    shared connected account. Automations with the shared key just get the shared account."""
    token = (request.headers.get("X-Google-Access-Token") or "").strip()
    if token and caller.kind == "google" and caller.email and os.environ.get("POST_DESK_GSC", "on").strip().lower() not in ("off", "none", "0", "false"):
        return PersonFirst(person_console(caller.email, token), shared)
    return shared


def _need_search(search):
    if search is None:
        why = f" Problem: {_search_problem}." if _search_problem else ""
        raise HTTPException(503, f"Search Console isn't set up on this service.{why}")


@app.get("/search/properties", summary="Search Console properties this service can read")
def search_properties(caller: Caller = Depends(require_caller), search: SearchConsole | None = Depends(get_user_search)):
    _need_search(search)
    try:
        sites = search.properties()
    except SearchConsoleError as e:
        raise HTTPException(502, {"code": e.code, "message": str(e)})
    return {"account": search.who, "properties": [{"property": x.get("siteUrl"), "permission": x.get("permissionLevel")} for x in sites]}


@app.get("/search/opportunities/{domain:path}", summary="A client's pages close to Google's first page")
def search_opportunities(domain: str, limit: int = 50, caller: Caller = Depends(require_caller),
                         search: SearchConsole | None = Depends(get_user_search)):
    _need_search(search)
    try:
        r = search.report(domain)
    except SearchConsoleError as e:
        raise HTTPException(404 if e.code == "no_property" else 502, {"code": e.code, "message": str(e)})
    return {"domain": r["domain"], "property": r["property"], "account": r["account"], "from": r["start"], "to": r["end"],
            "pages_with_data": len(r["pages"]), "settings": r["settings"], "close_to_page_one": len(r["opportunities"]),
            "pages": [describe(s, True) for s in r["opportunities"][:max(1, min(limit, 500))]]}


class CopyRequest(BaseModel):
    html: str = Field(min_length=1, max_length=copy_page.MAX_CHARS)
    title: str = Field(default="", max_length=300)


@app.post("/copy", summary="Save a post's HTML on a page with a Copy button (for the Google Doc side panel)")
def save_copy(req: CopyRequest, request: Request, caller: Caller = Depends(require_caller),
              store: IndexStore | None = Depends(get_store)):
    if store is None:
        raise HTTPException(503, "Storage isn't set up on this service.")
    copy_id = copy_page.new_id()
    try:
        store.put_text(copy_page.key(copy_id), copy_page.record(req.html, req.title, caller.email))
    except Exception as e:
        log(event="copy_error", error=f"{type(e).__name__}: {e}", user=caller.email)
        raise HTTPException(502, {"code": "storage_failed", "message": f"Couldn't save the HTML ({type(e).__name__})."})
    base = str(request.base_url).rstrip("/").replace("http://", "https://", 1) if "localhost" not in str(request.base_url) else str(request.base_url).rstrip("/")
    return {"id": copy_id, "url": f"{base}/copy/{copy_id}", "expires_days": copy_page.KEEP_DAYS}


@app.get("/copy/{copy_id}", response_class=HTMLResponse, include_in_schema=False)
def show_copy(copy_id: str, store: IndexStore | None = Depends(get_store)):
    gone = HTMLResponse("<!doctype html><meta charset=utf-8><title>Expired</title><p style='font:16px Arial;margin:40px'>"
                        "This link has expired or doesn't exist. Run Copy as HTML again in the Google Doc.</p>", status_code=404)
    if store is None or not copy_page.valid_id(copy_id):
        return gone
    try:
        raw = store.get_text(copy_page.key(copy_id))
    except Exception:
        raw = None
    rec = json.loads(raw) if raw else None
    if not rec or not copy_page.fresh(rec):
        return gone
    nonce = copy_page.new_id()
    return HTMLResponse(copy_page.page(rec, nonce), headers={
        "Content-Security-Policy": f"default-src 'none'; style-src 'unsafe-inline'; script-src 'nonce-{nonce}'; frame-src 'self' about:; "
                                   "child-src 'self' about:; base-uri 'none'; form-action 'none'",
        "X-Robots-Tag": "noindex", "Referrer-Policy": "no-referrer", "Cache-Control": "private, no-store"})


@app.get("/health")
def health():
    return {"ok": True, "version": VERSION}


@app.post("/generate", response_model=GenerateResponse)
def generate_route(req: GenerateRequest, model: JSONModel = Depends(pick_model), caller: Caller = Depends(require_caller),
                   store: IndexStore | None = Depends(get_store), embedder: Embedder | None = Depends(get_embedder),
                   search: SearchConsole | None = Depends(get_user_search)):
    who = {"auth": caller.kind, "user": caller.email}
    body = req.model_dump(mode="json")
    urls_from = "request"
    if not body["sitemap_urls"]:
        body["sitemap_urls"], urls_from = _client_urls(req.client.domain, store)
    # Logged at the start so a slow or stuck run shows up straight away (Cloud Run logs requests only when they end).
    log(event="generate_start", client=req.client.name, urls=len(body["sitemap_urls"]), urls_from=urls_from, chars=len(req.post.content),
        model=getattr(model, "name", "?"), provider=getattr(model, "provider", None), **who)
    try:
        return generate(body, model, lambda **kw: log(**kw, **who), store=store, embedder=embedder, search=search)
    except ModelError as e:
        t = getattr(e, "timings", None) or {}
        where = (f" Reading the site and Search Console took {t.get('retrieval_ms', 0) / 1000:.0f}s, the AI models "
                 f"{t.get('model_ms', 0) / 1000:.0f}s.") if t else ""
        log(event="model_error", code=e.code, error=str(e), timings=t, **who)
        status = {"rate_limited": 429, "model_timeout": 504}.get(e.code, 502)
        raise HTTPException(status, {"code": e.code, "message": str(e) + where, "timings": t})


SITEMAP_CACHE_S = 6 * 3600
_sitemaps: dict[str, tuple[float, list[str]]] = {}


def _client_urls(domain: str, store) -> tuple[list[str], str]:
    """The client's pages for callers (like the Google Doc) that don't send a sitemap: from their index if they
    have one, otherwise from their sitemap, found automatically and kept for a few hours."""
    d = normalise_domain(domain)
    if not d:
        raise HTTPException(422, {"code": "no_pages", "message": "Give the client's domain, or send sitemap_urls."})
    try:
        index = store.load(d) if store else None
    except Exception as e:
        log(event="index_error", domain=d, error=f"{type(e).__name__}: {e}")
        index = None
    urls = [u for u in (index.pages if index else {}) if not unlinkable(index, u)]
    if urls:
        return urls, "index"
    hit = _sitemaps.get(d)
    if hit and time.monotonic() - hit[0] < SITEMAP_CACHE_S:
        return hit[1], "sitemap"
    try:
        found, urls = asyncio.run(find_sitemap(d))
    except (Blocked, httpx.HTTPError) as e:
        raise HTTPException(422, {"code": "no_pages", "message": f"Couldn't find a sitemap for {d} ({e}). Send sitemap_urls, "
                                                              "or index the site with POST /index and its sitemap_url."})
    log(event="sitemap_found", domain=d, sitemap=found, urls=len(urls))
    _sitemaps[d] = (time.monotonic(), urls)
    return urls, "sitemap"


def _property_domain(site: str) -> str:
    return normalise_domain(site.removeprefix("sc-domain:"))


@app.get("/clients", summary="Clients for pickers such as the Google Doc sidebar: indexed sites plus Search Console properties")
def clients(caller: Caller = Depends(require_caller), store: IndexStore | None = Depends(get_store),
            search: SearchConsole | None = Depends(get_user_search)):
    indexed, problems = set(), []
    if store is not None:
        try:
            indexed = set(store.domains())
        except Exception as e:
            log(event="index_error", error=f"{type(e).__name__}: {e}", user=caller.email)
            problems.append(f"Couldn't list the indexes ({type(e).__name__}).")
    in_search = set()
    if search is not None:
        try:
            in_search = {_property_domain(p.get("siteUrl", "")) for p in search.properties()
                         if p.get("permissionLevel") != "siteUnverifiedUser"} - {""}
        except Exception as e:
            log(event="search_console_skipped", error=f"{type(e).__name__}: {e}", user=caller.email)
            problems.append(f"Couldn't read Search Console ({e}).")
    out = {"clients": [{"domain": d, "indexed": d in indexed, "search_console": d in in_search} for d in sorted(indexed | in_search)]}
    if problems:
        out["problems"] = problems
    return out


def _indexing(store, embedder):
    if store is None or embedder is None:
        why = f" Problem: {_store_problem}." if _store_problem else ""
        raise HTTPException(503, f"Page indexing isn't set up on this service (it needs POST_DESK_BUCKET and Vertex AI).{why}")


@app.post("/index", response_model=IndexStats,
          summary="Crawl and embed a client's pages",
          description="Reads each page in the sitemap and stores an embedding of it, so link candidates can be chosen by meaning. "
                      "Each call works for up to about 3 minutes; call again until `remaining` is 0. Unchanged pages aren't re-embedded.")
async def index_route(req: IndexRequest, caller: Caller = Depends(require_caller),
                      store: IndexStore | None = Depends(get_store), embedder: Embedder | None = Depends(get_embedder)):
    _indexing(store, embedder)
    domain = normalise_domain(req.client.domain)
    if not domain:
        raise HTTPException(422, "client.domain is needed to index a site.")
    budget = req.budget_s or float(os.environ.get("POST_DESK_INDEX_BUDGET_S", 200))
    urls = list(req.sitemap_urls)
    if req.sitemap_url:
        try:
            urls += await fetch_sitemap(req.sitemap_url, domain)
        except (Blocked, httpx.HTTPError) as e:
            raise HTTPException(422, {"code": "sitemap_unreadable", "message": f"Couldn't read the sitemap ({type(e).__name__}: {e})."})
    if not urls:
        try:
            found, urls = await find_sitemap(domain)
            log(event="sitemap_found", domain=domain, sitemap=found, urls=len(urls))
        except (Blocked, httpx.HTTPError) as e:
            raise HTTPException(422, {"code": "sitemap_unreadable", "message": f"Couldn't find a sitemap for {domain} ({e}). Give its sitemap_url."})
    try:
        return await build_index(domain, urls, store, embedder, budget_s=budget,
                                 log=lambda **kw: log(**kw, auth=caller.kind, user=caller.email))
    except EmbedError as e:
        log(event="index_error", domain=domain, error=str(e), user=caller.email)
        raise HTTPException(502, {"code": "embedding_failed", "message": str(e)})
    except Exception as e:   # most likely Cloud Storage: missing bucket or permission
        log(event="index_error", domain=domain, error=f"{type(e).__name__}: {e}", user=caller.email)
        raise HTTPException(502, {"code": "index_storage_failed", "message": f"Couldn't read or save the index ({type(e).__name__}: {e})."})


@app.get("/index/{domain:path}", summary="What's in a client's page index")
def index_status(domain: str, caller: Caller = Depends(require_caller), store: IndexStore | None = Depends(get_store)):
    if store is None:
        raise HTTPException(503, "Page indexing isn't set up on this service.")
    try:
        index = store.load(normalise_domain(domain))
    except Exception as e:
        log(event="index_error", domain=domain, error=f"{type(e).__name__}: {e}", user=caller.email)
        raise HTTPException(502, {"code": "index_storage_failed", "message": f"Couldn't read the index ({type(e).__name__}: {e})."})
    return summarise(index, domain)
