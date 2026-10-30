"""Search Console: finding the property, working out which pages are close to page one, and using that in link picks.
A stand-in session plays Google's part, so nothing here touches the network."""
import datetime as dt
import json

import pytest

from app import gsc
from app.gsc import SearchConsole, SearchConsoleError, aggregate, choose_property, opportunities, page_key

SITE = "https://www.example-landscapes.co.uk"


class Resp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body
        self.text = json.dumps(body)

    def json(self):
        return self._body


class FakeSession:
    """Plays Search Console. `rows` are page+query rows; `sites` the properties the account can see."""

    def __init__(self, sites, rows=(), status=200, page_size=None):
        self.sites, self.rows, self.status, self.page_size = sites, list(rows), status, page_size
        self.calls = []

    def request(self, method, url, timeout=None, json=None):
        self.calls.append((method, url, json))
        if self.status != 200:
            return Resp(self.status, {"error": {"message": "nope"}})
        if url.endswith("/sites"):
            return Resp(200, {"siteEntry": self.sites})
        size = self.page_size or json["rowLimit"]
        start = json["startRow"]
        return Resp(200, {"rows": self.rows[start:start + size]})


def row(page, query, impressions, position, clicks=0):
    return {"keys": [page, query], "impressions": impressions, "position": position, "clicks": clicks}


ROWS = [
    row(SITE + "/services/patio-installation/", "patio installation watford", 900, 11.0, 12),
    row(SITE + "/services/patio-installation/", "patio layers near me", 300, 15.0, 2),
    row(SITE + "/services/garden-design/", "garden design watford", 2000, 3.2, 300),     # already on page one
    row(SITE + "/services/fencing/", "fence installers", 40, 12.0),                      # close, but too few impressions
    row(SITE + "/blog/decking-ideas/", "decking ideas", 5000, 34.0, 4),                  # too far down
    row("http://example-landscapes.co.uk/services/fencing", "closeboard fencing", 30, 9.0),
]
SITES = [{"siteUrl": "https://www.example-landscapes.co.uk/", "permissionLevel": "siteFullUser"},
         {"siteUrl": "sc-domain:example-landscapes.co.uk", "permissionLevel": "siteOwner"},
         {"siteUrl": "sc-domain:other.co.uk", "permissionLevel": "siteOwner"}]


# ---------- the maths ----------
def test_position_is_weighted_by_impressions():
    p = aggregate(ROWS)[page_key(SITE + "/services/patio-installation")]
    assert p.impressions == 1200 and p.clicks == 14
    assert p.position == pytest.approx((900 * 11 + 300 * 15) / 1200)          # 12.0
    assert p.top_queries() == ["patio installation watford", "patio layers near me"]


def test_page_urls_match_whatever_the_form():
    assert page_key("https://www.x.co.uk/a/") == page_key("http://x.co.uk/a") == page_key("https://X.co.uk/A?utm=1#h")
    pages = aggregate(ROWS)
    assert pages[page_key(SITE + "/services/fencing/")].impressions == 70        # both fencing URL forms merged


def test_close_to_page_one_means_position_8_to_20_with_real_demand():
    got = [page_key(s.url) for s in opportunities(aggregate(ROWS), 8, 20, 50)]
    assert got == [page_key(SITE + "/services/patio-installation"), page_key(SITE + "/services/fencing")]


def test_settings_change_the_window(monkeypatch):
    monkeypatch.setenv("POST_DESK_GSC_MIN_IMPRESSIONS", "1000")
    cfg = gsc.settings()
    assert opportunities(aggregate(ROWS), cfg["min_pos"], cfg["max_pos"], cfg["min_impressions"])[0].impressions == 1200
    assert len(opportunities(aggregate(ROWS), cfg["min_pos"], cfg["max_pos"], cfg["min_impressions"])) == 1


# ---------- finding the property ----------
def test_domain_property_preferred():
    assert choose_property(SITES, "https://www.example-landscapes.co.uk") == "sc-domain:example-landscapes.co.uk"


def test_url_prefix_property_when_no_domain_property():
    sites = [{"siteUrl": "http://example-landscapes.co.uk/", "permissionLevel": "siteFullUser"},
             {"siteUrl": "https://www.example-landscapes.co.uk/", "permissionLevel": "siteRestrictedUser"},
             {"siteUrl": "https://www.example-landscapes.co.uk/blog/", "permissionLevel": "siteOwner"}]
    assert choose_property(sites, "example-landscapes.co.uk") == "https://www.example-landscapes.co.uk/"


def test_unverified_properties_are_ignored():
    sites = [{"siteUrl": "sc-domain:example-landscapes.co.uk", "permissionLevel": "siteUnverifiedUser"}]
    assert choose_property(sites, "example-landscapes.co.uk") is None


def test_other_clients_properties_are_never_used():
    assert choose_property([{"siteUrl": "sc-domain:other.co.uk", "permissionLevel": "siteOwner"}], "example-landscapes.co.uk") is None


# ---------- talking to Google ----------
def test_report_asks_for_90_final_days_of_web_page_and_query_data():
    s = FakeSession(SITES, ROWS)
    r = SearchConsole(session=s, who="test").report("example-landscapes.co.uk", today=dt.date(2026, 10, 26))
    assert r["property"] == "sc-domain:example-landscapes.co.uk" and (r["start"], r["end"]) == ("2026-07-26", "2026-10-23")
    method, url, body = s.calls[1]
    assert url.endswith("/sites/sc-domain%3Aexample-landscapes.co.uk/searchAnalytics/query")
    assert body["dimensions"] == ["page", "query"] and body["type"] == "web" and body["dataState"] == "final"
    assert len(r["opportunities"]) == 2


def test_large_sites_are_paged(monkeypatch):
    monkeypatch.setattr(gsc, "ROW_LIMIT", 2)
    s = FakeSession(SITES, ROWS, page_size=2)
    r = SearchConsole(session=s).report("example-landscapes.co.uk")
    assert len([c for c in s.calls if c[0] == "POST"]) == 4 and len(r["pages"]) == 4    # 6 rows, 2 per page, last page short


def test_reports_are_cached():
    s = FakeSession(SITES, ROWS)
    sc = SearchConsole(session=s)
    sc.report("example-landscapes.co.uk")
    sc.report("www.example-landscapes.co.uk")
    assert len(s.calls) == 2                                       # one list + one query, not four


def test_failures_are_cached_briefly_so_posts_stay_fast():
    s = FakeSession([{"siteUrl": "sc-domain:other.co.uk", "permissionLevel": "siteOwner"}])
    sc = SearchConsole(session=s, who="service account x")
    for _ in range(3):
        with pytest.raises(SearchConsoleError) as e:
            sc.report("example-landscapes.co.uk")
    assert e.value.code == "no_property" and "service account x" in str(e.value) and len(s.calls) == 1


@pytest.mark.parametrize("status, code", [(401, "signed_out"), (403, "no_access"), (500, "api_error")])
def test_google_errors_are_explained(status, code):
    with pytest.raises(SearchConsoleError) as e:
        SearchConsole(session=FakeSession(SITES, status=status)).properties()
    assert e.value.code == code


def test_unreachable():
    class Down:
        def request(self, *a, **kw):
            raise ConnectionError("no route")
    with pytest.raises(SearchConsoleError) as e:
        SearchConsole(session=Down()).properties()
    assert e.value.code == "unreachable"


# ---------- signing in ----------
def test_connected_account_is_used_when_saved(monkeypatch):
    monkeypatch.setenv("POST_DESK_GSC_CREDENTIALS", json.dumps(
        {"type": "authorized_user", "client_id": "id", "client_secret": "s", "refresh_token": "r", "account": "owner@gmail.com"}))
    session, who = gsc.make_session()
    assert who == "connected account owner@gmail.com"
    assert session.credentials.refresh_token == "r" and gsc.SCOPE in session.credentials.scopes


def test_broken_saved_sign_in_is_explained(monkeypatch):
    monkeypatch.setenv("POST_DESK_GSC_CREDENTIALS", json.dumps({"client_id": "id"}))
    with pytest.raises(SearchConsoleError, match="missing client_secret, refresh_token"):
        gsc.make_session()
    monkeypatch.setenv("POST_DESK_GSC_CREDENTIALS", "not json")
    with pytest.raises(SearchConsoleError, match="isn't valid JSON"):
        gsc.make_session()


def test_service_account_is_used_otherwise(monkeypatch):
    monkeypatch.delenv("POST_DESK_GSC_CREDENTIALS", raising=False)
    seen = {}

    class SA:
        service_account_email = "post-desk-runtime@my-project.iam.gserviceaccount.com"

    import google.auth
    monkeypatch.setattr(google.auth, "default", lambda scopes=None: (seen.setdefault("scopes", scopes) and SA(), "p"))
    _, who = gsc.make_session()
    assert seen["scopes"] == [gsc.SCOPE] and who.endswith("post-desk-runtime@my-project.iam.gserviceaccount.com")
