"""Fetches a client's pages and pulls out what a page is about: title, H1, meta description and main text.

The service crawls URLs that callers send it, so it guards against being pointed somewhere it shouldn't go:
only http(s), only the client's own domain (and its www. form), only public IP addresses (never the
metadata server or anything internal), the same checks on every redirect, robots.txt respected,
HTML only, and caps on size and time.
"""
from __future__ import annotations

import asyncio
import gzip
import ipaddress
import re
import socket
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import httpx

BOT_NAME = "PostDeskBot"
# Standard bot form: "compatible" with a name and an info link, as search engine crawlers use.
USER_AGENT = "Mozilla/5.0 (compatible; PostDeskBot/0.5; +https://github.com/cloudnxne/post-desk)"
MAX_BYTES = 2_000_000
MAX_REDIRECTS = 3
TIMEOUT = httpx.Timeout(15.0, connect=5.0)
TEXT_CAP = 4000


class Blocked(Exception):
    """A URL the crawler refuses to fetch. The message is the reason, used in the index stats."""


def normalise_domain(domain: str) -> str:
    """'https://www.Example.co.uk/path' -> 'example.co.uk'"""
    d = (domain or "").strip().lower()
    d = re.sub(r"^[a-z]+://", "", d).split("/")[0].split(":")[0]
    return d[4:] if d.startswith("www.") else d


def allowed_hosts(domain: str) -> set[str]:
    d = normalise_domain(domain)
    return {d, "www." + d} if d else set()


def _public(ip: str) -> bool:
    a = ipaddress.ip_address(ip)
    return a.is_global and not a.is_multicast


def resolve(host: str) -> list[str]:
    """All addresses the host resolves to. Replaced in tests."""
    return list({info[4][0] for info in socket.getaddrinfo(host, None)})


def check_url(url: str, hosts: set[str]) -> None:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise Blocked("not_http")
    host = (parts.hostname or "").lower()
    if host not in hosts:
        raise Blocked("other_domain")
    if parts.port not in (None, 80, 443):
        raise Blocked("unusual_port")
    try:
        ips = resolve(host)
    except OSError:
        raise Blocked("dns_failed")
    if not ips or not all(_public(ip) for ip in ips):
        raise Blocked("private_address")


@dataclass
class Fetched:
    url: str           # final URL after redirects
    status: int
    content_type: str
    body: str
    raw: bytes = b""


async def safe_get(client: httpx.AsyncClient, url: str, hosts: set[str], html_only: bool = True) -> Fetched:
    for _ in range(MAX_REDIRECTS + 1):
        check_url(url, hosts)
        async with client.stream("GET", url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,*/*;q=0.5"}) as r:
            if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
                url = urljoin(url, r.headers["location"])
                continue
            ctype = r.headers.get("content-type", "").split(";")[0].strip().lower()
            if html_only and r.status_code == 200 and ctype not in ("text/html", "application/xhtml+xml"):
                raise Blocked("not_html")
            chunks, size = [], 0
            async for chunk in r.aiter_bytes():
                size += len(chunk)
                if size > MAX_BYTES:
                    raise Blocked("too_large")
                chunks.append(chunk)
            enc = r.encoding or "utf-8"
            raw = b"".join(chunks)
            return Fetched(url=str(r.url), status=r.status_code, content_type=ctype,
                           body=raw.decode(enc, errors="replace"), raw=raw)
    raise Blocked("too_many_redirects")


# ---------- extraction ----------
_SKIP = {"script", "style", "noscript", "svg", "template", "nav", "header", "footer", "form", "aside", "iframe"}
_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}


class _PageParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title, self.h1, self.meta, self.og = [], [], "", ""
        self.noindex = False
        self.all_text, self.main_text = [], []
        self._skip = 0
        self._stack: list[str] = []
        self._in_title = self._in_h1 = False
        self._main = 0
        self._h1_done = False

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "meta":
            name = (a.get("name") or a.get("property") or "").lower()
            if name == "description":
                self.meta = a.get("content", "").strip()
            elif name == "og:description":
                self.og = a.get("content", "").strip()
            elif name in ("robots", "googlebot") and "noindex" in a.get("content", "").lower():
                self.noindex = True
            return
        if tag in _VOID:
            return
        self._stack.append(tag)
        if tag in _SKIP:
            self._skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag == "h1" and not self._h1_done:
            self._in_h1 = True
        elif tag in ("main", "article"):
            self._main += 1

    def handle_endtag(self, tag):
        if tag in _VOID or tag not in self._stack:
            return
        while self._stack:  # close anything left open inside this tag too
            t = self._stack.pop()
            if t in _SKIP:
                self._skip -= 1
            elif t == "title":
                self._in_title = False
            elif t == "h1" and self._in_h1:
                self._in_h1, self._h1_done = False, True
            elif t in ("main", "article"):
                self._main -= 1
            if t == tag:
                break

    def handle_data(self, data):
        if self._in_title:
            self.title.append(data)
            return
        if self._skip:
            return
        if self._in_h1:
            self.h1.append(data)
        self.all_text.append(data)
        if self._main:
            self.main_text.append(data)


def _squash(parts: list[str]) -> str:
    return re.sub(r"\s+", " ", " ".join(parts)).strip()


def extract(html: str) -> dict:
    p = _PageParser()
    p.feed(html)
    p.close()
    main = _squash(p.main_text)
    text = main if len(main) >= 200 else _squash(p.all_text)
    return {
        "title": _squash(p.title)[:300],
        "h1": _squash(p.h1)[:300],
        "meta": (p.meta or p.og)[:500],
        "text": text[:TEXT_CAP],
        "noindex": p.noindex,
    }


def summary(page: dict, limit: int = 220) -> str:
    """One or two sentences on what the page is about, for the prompt."""
    s = page.get("meta") or page.get("text") or ""
    if len(s) <= limit:
        return s
    cut = s[:limit]
    return (cut[: cut.rfind(" ")] if " " in cut else cut).rstrip(" ,.;:") + "…"


def embed_text(page: dict) -> str:
    """The text that represents a page in the index."""
    return "\n".join(x for x in (page.get("title"), page.get("h1"), page.get("meta"), (page.get("text") or "")[:3000]) if x)


# ---------- crawling ----------
# Failures that say "not now" rather than "not this page": retried on the next indexing run.
TRANSIENT = {"timeout", "connect_failed", "fetch_failed", "http_429", "http_500", "http_502", "http_503", "http_504", "dns_failed"}
# This many of those in a row means the site (or its firewall) has started refusing us: stop for this run.
BACKOFF_AFTER = 5


@dataclass
class CrawlResult:
    url: str
    ok: bool
    reason: str = ""                           # why it was skipped
    page: dict = field(default_factory=dict)   # title, h1, meta, text
    error: str = ""                            # detail for transient failures, for the stats


@dataclass
class CrawlRun:
    results: list[CrawlResult]
    stopped_early: str = ""                    # why the run stopped before the deadline, if it did


class Robots:
    """robots.txt per host, fetched once. Missing or unreadable robots.txt means everything is allowed.
    A Crawl-delay in robots.txt is honoured (up to 10 seconds)."""

    def __init__(self, client: httpx.AsyncClient, hosts: set[str]):
        self.client, self.hosts = client, hosts
        self._cache: dict[str, RobotFileParser | None] = {}
        self._lock = asyncio.Lock()

    async def _get(self, url: str) -> RobotFileParser | None:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        async with self._lock:
            if origin not in self._cache:
                rp = None
                try:
                    f = await safe_get(self.client, origin + "/robots.txt", self.hosts, html_only=False)
                    if f.status == 200:
                        rp = RobotFileParser()
                        rp.parse(f.body.splitlines())
                except (Blocked, httpx.HTTPError):
                    pass
                self._cache[origin] = rp
        return self._cache[origin]

    async def allowed(self, url: str) -> bool:
        rp = await self._get(url)
        return rp is None or rp.can_fetch(BOT_NAME, url)

    async def delay(self, url: str) -> float:
        rp = await self._get(url)
        d = rp.crawl_delay(BOT_NAME) if rp else None
        return min(float(d), 10.0) if d else 0.0


class _Pacer:
    """Spaces out requests to the site: at least `gap` seconds between the start of one request and the next."""

    def __init__(self, gap: float):
        self.gap, self._next = gap, 0.0
        self._lock = asyncio.Lock()

    async def wait(self):
        async with self._lock:
            loop = asyncio.get_running_loop()
            now = loop.time()
            if self._next > now:
                await asyncio.sleep(self._next - now)
            self._next = max(now, self._next) + self.gap


def _failure(e: Exception) -> tuple[str, str]:
    if isinstance(e, httpx.TimeoutException):
        return "timeout", type(e).__name__
    if isinstance(e, httpx.ConnectError):
        return "connect_failed", str(e)[:120] or type(e).__name__
    return "fetch_failed", f"{type(e).__name__}: {str(e)[:120]}"


async def crawl(urls: list[str], domain: str, concurrency: int | None = None, delay: float | None = None,
                deadline: float | None = None, transport: httpx.AsyncBaseTransport | None = None) -> CrawlRun:
    """Crawls the URLs politely: a couple at a time, spaced out, stopping early (without error) at the deadline
    (event loop time) or when the site keeps refusing. URLs not reached are missing from the results,
    so the next run picks them up."""
    import os

    concurrency = concurrency or int(os.environ.get("POST_DESK_CRAWL_CONCURRENCY", 2))
    delay = float(os.environ.get("POST_DESK_CRAWL_DELAY_S", 0.5)) if delay is None else delay
    hosts = allowed_hosts(domain)
    sem = asyncio.Semaphore(concurrency)
    loop = asyncio.get_running_loop()
    state = {"in_a_row": 0, "stop": ""}

    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=False, transport=transport) as client:
        robots = Robots(client, hosts)
        pacer = _Pacer(delay)
        if urls:
            try:
                pacer.gap = max(delay, await robots.delay(urls[0]))
            except Exception:
                pass

        async def attempt(url: str) -> CrawlResult:
            try:
                check_url(url, hosts)
                if not await robots.allowed(url):
                    return CrawlResult(url, False, "robots_txt")
                await pacer.wait()
                f = await safe_get(client, url, hosts)
            except Blocked as e:
                return CrawlResult(url, False, str(e))
            except httpx.HTTPError as e:
                reason, detail = _failure(e)
                return CrawlResult(url, False, reason, error=detail)
            if f.status != 200:
                return CrawlResult(url, False, f"http_{f.status}")
            page = extract(f.body)
            if page["noindex"]:
                return CrawlResult(url, False, "noindex")
            if not (page["title"] or page["text"]):
                return CrawlResult(url, False, "empty")
            return CrawlResult(url, True, page=page)

        async def one(url: str) -> CrawlResult | None:
            async with sem:
                if state["stop"] or (deadline is not None and loop.time() > deadline):
                    return None
                r = await attempt(url)
                if r.reason in TRANSIENT and not state["stop"]:
                    await asyncio.sleep(min(10.0, 2.0 + pacer.gap * 4))   # one gentle retry after a pause
                    r = await attempt(url)
                if r.reason in TRANSIENT:
                    state["in_a_row"] += 1
                    if state["in_a_row"] >= BACKOFF_AFTER and not state["stop"]:
                        state["stop"] = (f"The site stopped answering after {BACKOFF_AFTER} failures in a row ({r.reason}), "
                                         "so the crawl paused. Its firewall may be limiting requests; run again later.")
                elif r.ok or r.reason:
                    state["in_a_row"] = 0
                return r

        results = await asyncio.gather(*(one(u) for u in urls))
    return CrawlRun([r for r in results if r is not None], state["stop"])


MAX_CHILD_SITEMAPS = 50


async def fetch_sitemap(sitemap_url: str, domain: str, transport: httpx.AsyncBaseTransport | None = None) -> list[str]:
    """Reads a sitemap (or a sitemap index and its child sitemaps, plain or .gz) on the client's own domain.
    Returns the page URLs. Raises Blocked if the sitemap itself can't be read."""
    from .core import parse_sitemap

    hosts = allowed_hosts(domain)
    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=False, transport=transport) as client:
        async def read(url: str) -> dict:
            f = await safe_get(client, url, hosts, html_only=False)
            if f.status != 200:
                raise Blocked(f"sitemap_http_{f.status}")
            data = f.raw
            if data[:2] == b"\x1f\x8b":
                data = gzip.decompress(data)
            return parse_sitemap(data.decode("utf-8", errors="replace"))

        top = await read(sitemap_url)
        if not top["is_index"]:
            return top["urls"]
        urls: list[str] = []
        for child in top["child_sitemaps"][:MAX_CHILD_SITEMAPS]:
            try:
                urls += (await read(child))["urls"]
            except (Blocked, httpx.HTTPError):
                continue    # one broken child sitemap shouldn't stop the rest
        return list(dict.fromkeys(urls))


SITEMAP_GUESSES = ("/sitemap_index.xml", "/sitemap.xml", "/wp-sitemap.xml", "/sitemap-index.xml")


async def find_sitemap(domain: str, transport: httpx.AsyncBaseTransport | None = None) -> tuple[str, list[str]]:
    """Finds a client's sitemap without being told where it is: the Sitemap lines in robots.txt first, then the
    usual addresses (Yoast, WordPress, generic), on the bare and www host. Returns (sitemap URL, page URLs).
    Raises Blocked("no_sitemap") if none of them gives any pages."""
    d = normalise_domain(domain)
    hosts = allowed_hosts(d)
    tried: list[str] = []
    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=False, transport=transport) as client:
        for host in ("www." + d, d):
            try:
                f = await safe_get(client, f"https://{host}/robots.txt", hosts, html_only=False)
            except (Blocked, httpx.HTTPError):
                continue
            if f.status == 200:
                text = f.raw.decode("utf-8", errors="replace")
                tried += [m.strip() for m in re.findall(r"(?im)^\s*sitemap:\s*(\S+)", text)]
                break
    for host in (d, "www." + d):
        tried += [f"https://{host}{p}" for p in SITEMAP_GUESSES]
    for url in dict.fromkeys(tried):
        try:
            urls = await fetch_sitemap(url, d, transport=transport)
        except (Blocked, httpx.HTTPError, ValueError):
            continue
        if urls:
            return url, urls
    raise Blocked("no_sitemap")
