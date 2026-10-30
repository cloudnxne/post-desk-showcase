"""Search Console data: which of a client's pages sit just off page one, so new posts can link to them.

Signs in one of two ways:
  1. A connected account: one person signs in once (scripts/connect_gsc.py) and the saved, read-only sign-in is
     passed in POST_DESK_GSC_CREDENTIALS (from Secret Manager). Reads every property that account can see.
  2. Otherwise the Cloud Run service account, which reads the properties it has been added to as a Restricted user.

For each client the service finds the matching property (domain property first, then URL-prefix ones), pulls page
and query totals for the last POST_DESK_GSC_DAYS days (default 90), and marks "close to page one" pages: average
position between POST_DESK_GSC_MIN_POS and POST_DESK_GSC_MAX_POS (default 8 to 20) with at least
POST_DESK_GSC_MIN_IMPRESSIONS impressions (default 50). Results are cached for 12 hours per domain.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import threading
import time
from dataclasses import dataclass, field
from urllib.parse import quote, urlsplit

from .crawl import normalise_domain

SCOPE = "https://www.googleapis.com/auth/webmasters.readonly"
API = "https://www.googleapis.com/webmasters/v3"
ROW_LIMIT = 25000
MAX_ROWS = 100000            # page+query rows read per client at most (4 requests)
CACHE_TTL = 12 * 3600
FAIL_TTL = 600               # don't retry a failing property more than every 10 minutes


class SearchConsoleError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def page_key(url: str) -> str:
    """Matches Search Console page URLs to sitemap URLs: ignores http/https, www, case, trailing slash, query and hash."""
    p = urlsplit((url or "").strip())
    host = (p.hostname or "").lower()
    host = host[4:] if host.startswith("www.") else host
    path = (p.path or "/").rstrip("/") or "/"
    return f"{host}{path.lower()}"


@dataclass
class PageStats:
    url: str
    clicks: float = 0
    impressions: float = 0
    _pos_x_impr: float = 0
    queries: dict[str, float] = field(default_factory=dict)   # query -> impressions

    @property
    def position(self) -> float:
        return self._pos_x_impr / self.impressions if self.impressions else 0.0

    def top_queries(self, n: int = 3) -> list[str]:
        return [q for q, _ in sorted(self.queries.items(), key=lambda x: -x[1])[:n]]


def settings() -> dict:
    env = os.environ.get
    return {"days": int(env("POST_DESK_GSC_DAYS", 90)), "min_pos": float(env("POST_DESK_GSC_MIN_POS", 8)),
            "max_pos": float(env("POST_DESK_GSC_MAX_POS", 20)), "min_impressions": float(env("POST_DESK_GSC_MIN_IMPRESSIONS", 50))}


def aggregate(rows: list[dict]) -> dict[str, PageStats]:
    """page+query rows -> one PageStats per page (position weighted by impressions, as Search Console does)."""
    pages: dict[str, PageStats] = {}
    for r in rows:
        keys = r.get("keys") or []
        if not keys:
            continue
        url = keys[0]
        s = pages.setdefault(page_key(url), PageStats(url=url))
        impr = float(r.get("impressions") or 0)
        s.clicks += float(r.get("clicks") or 0)
        s.impressions += impr
        s._pos_x_impr += float(r.get("position") or 0) * impr
        if len(keys) > 1 and keys[1]:
            s.queries[keys[1]] = s.queries.get(keys[1], 0) + impr
    return pages


def opportunities(pages: dict[str, PageStats], min_pos=8, max_pos=20, min_impressions=50) -> list[PageStats]:
    """Close to page one, with real demand: the pages a new internal link is most likely to help. Most impressions first."""
    hits = [s for s in pages.values() if min_pos <= s.position <= max_pos and s.impressions >= min_impressions]
    return sorted(hits, key=lambda s: -s.impressions)


def choose_property(sites: list[dict], domain: str) -> str | None:
    """The client's property: the domain property if the account has one, else the best URL-prefix one (https, www)."""
    d = normalise_domain(domain)
    readable = [s for s in sites if s.get("permissionLevel") != "siteUnverifiedUser"]
    for s in readable:
        if s.get("siteUrl") == f"sc-domain:{d}":
            return s["siteUrl"]
    prefixed = []
    for s in readable:
        u = s.get("siteUrl", "")
        host = (urlsplit(u).hostname or "").lower()
        if host in (d, "www." + d) and (urlsplit(u).path or "/") == "/":
            prefixed.append(u)
    prefixed.sort(key=lambda u: (not u.startswith("https://"), "://www." not in u))
    return prefixed[0] if prefixed else None


# ---------- talking to Google ----------
def connected_account_info() -> dict | None:
    raw = (os.environ.get("POST_DESK_GSC_CREDENTIALS") or "").strip()
    if not raw:
        return None
    try:
        info = json.loads(raw)
    except json.JSONDecodeError:
        raise SearchConsoleError("bad_credentials", "POST_DESK_GSC_CREDENTIALS isn't valid JSON.")
    missing = [k for k in ("client_id", "client_secret", "refresh_token") if not info.get(k)]
    if missing:
        raise SearchConsoleError("bad_credentials", f"The saved Search Console sign-in is missing {', '.join(missing)}.")
    return info


def make_session():
    """An authorised HTTP session, and a label for which account is reading."""
    from google.auth.transport.requests import AuthorizedSession

    info = connected_account_info()
    if info:
        from google.oauth2.credentials import Credentials

        creds = Credentials.from_authorized_user_info(info, scopes=[SCOPE])
        return AuthorizedSession(creds), f"connected account{' ' + info['account'] if info.get('account') else ''}"
    import google.auth

    creds, _ = google.auth.default(scopes=[SCOPE])
    return AuthorizedSession(creds), f"service account {getattr(creds, 'service_account_email', '')}".strip()


class SearchConsole:
    def __init__(self, session=None, who: str = ""):
        if session is None:
            session, who = make_session()
        self.session, self.who = session, who
        self._cache: dict[str, tuple[float, dict]] = {}
        self._lock = threading.Lock()

    def _call(self, method: str, url: str, **kw) -> dict:
        try:
            r = self.session.request(method, url, timeout=(5, 60), **kw)
        except Exception as e:
            raise SearchConsoleError("unreachable", f"Couldn't reach Search Console ({type(e).__name__}: {e}).") from e
        if r.status_code == 401:
            fix = "Reopen Post Desk to sign in again." if isinstance(self.session, TokenSession) else "Run connect_gsc.py again."
            raise SearchConsoleError("signed_out", f"The Search Console sign-in has expired or been revoked. {fix}")
        if r.status_code == 403:
            raise SearchConsoleError("no_access", f"The {self.who or 'account'} can't read this Search Console property.")
        if r.status_code >= 400:
            raise SearchConsoleError("api_error", f"Search Console said {r.status_code}: {r.text[:200]}")
        return r.json()

    def properties(self) -> list[dict]:
        return self._call("GET", f"{API}/sites").get("siteEntry", [])

    def rows(self, site: str, start: dt.date, end: dt.date) -> list[dict]:
        out, start_row = [], 0
        while start_row < MAX_ROWS:
            body = {"startDate": start.isoformat(), "endDate": end.isoformat(), "dimensions": ["page", "query"],
                    "type": "web", "rowLimit": ROW_LIMIT, "startRow": start_row, "dataState": "final"}
            batch = self._call("POST", f"{API}/sites/{quote(site, safe='')}/searchAnalytics/query", json=body).get("rows", [])
            out += batch
            if len(batch) < ROW_LIMIT:
                break
            start_row += ROW_LIMIT
        return out

    def report(self, domain: str, today: dt.date | None = None) -> dict:
        """Everything Post Desk needs for one client, cached: the property used, every page's stats, and the opportunities.
        Raises SearchConsoleError (cached briefly too, so a broken property doesn't slow every post)."""
        d = normalise_domain(domain)
        with self._lock:
            hit = self._cache.get(d)
            if hit and time.time() < hit[0]:
                if isinstance(hit[1], SearchConsoleError):
                    raise hit[1]
                return hit[1]
        try:
            site = choose_property(self.properties(), d)
            if not site:
                raise SearchConsoleError("no_property", f"The {self.who or 'account'} has no Search Console property for {d}.")
            cfg = settings()
            end = (today or dt.date.today()) - dt.timedelta(days=3)          # recent days aren't final yet
            start = end - dt.timedelta(days=cfg["days"] - 1)
            pages = aggregate(self.rows(site, start, end))
            report = {"domain": d, "property": site, "account": self.who, "start": start.isoformat(), "end": end.isoformat(),
                      "pages": pages, "opportunities": opportunities(pages, cfg["min_pos"], cfg["max_pos"], cfg["min_impressions"]),
                      "settings": cfg}
        except SearchConsoleError as e:
            with self._lock:
                self._cache[d] = (time.time() + FAIL_TTL, e)
            raise
        with self._lock:
            self._cache[d] = (time.time() + CACHE_TTL, report)
        return report


class TokenSession:
    """Calls Google with a person's own short-lived access token, sent by the Google Doc for that request only."""

    def __init__(self, token: str):
        self.token = token

    def request(self, method, url, timeout=None, **kw):
        import requests

        headers = {**kw.pop("headers", {}), "Authorization": f"Bearer {self.token}"}
        return requests.request(method, url, headers=headers, timeout=timeout, **kw)


_people: dict[str, "SearchConsole"] = {}
_people_lock = threading.Lock()


def person_console(email: str, token: str) -> "SearchConsole":
    """One SearchConsole per person, so their reports are cached separately and nobody sees another person's properties.
    The token is replaced on every request and never stored anywhere else."""
    with _people_lock:
        sc = _people.get(email)
        if sc is None:
            sc = _people[email] = SearchConsole(session=TokenSession(token), who=f"your account ({email})")
        else:
            sc.session.token = token
        return sc


class PersonFirst:
    """The person's own Search Console first; the shared connected account only for clients their account can't see,
    or if their own sign-in can't be used (for example the Search Console permission wasn't given)."""

    def __init__(self, person: "SearchConsole", shared: "SearchConsole | None"):
        self.person, self.shared = person, shared
        self.who = person.who

    def properties(self) -> list[dict]:
        try:
            return self.person.properties()
        except SearchConsoleError:
            if self.shared is None:
                raise
            return self.shared.properties()

    def report(self, domain: str, today: dt.date | None = None) -> dict:
        try:
            return self.person.report(domain, today)
        except SearchConsoleError:
            if self.shared is None:
                raise
            return self.shared.report(domain, today)


def describe(s: PageStats, close: bool) -> dict:
    return {"url": s.url, "position": round(s.position, 1), "impressions": int(s.impressions), "clicks": int(s.clicks),
            "top_searches": s.top_queries(), "close_to_page_one": close}
