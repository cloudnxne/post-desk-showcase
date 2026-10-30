"""Each person's own Search Console: the Google Doc sends their access token, their own properties and data are used,
cached separately per person, with the shared connected account as the fallback."""
import pytest

from app import gsc
from app.auth import Caller, require_caller
from app.gsc import PersonFirst, SearchConsoleError, TokenSession, person_console
from app.main import app, get_search


class Fake:
    def __init__(self, who, sites=(), fail=None):
        self.who, self.sites, self.fail = who, list(sites), fail
        self.reports = []

    def properties(self):
        if self.fail:
            raise SearchConsoleError(self.fail, "nope")
        return [{"siteUrl": s, "permissionLevel": "siteOwner"} for s in self.sites]

    def report(self, domain, today=None):
        self.reports.append(domain)
        if self.fail or not any(domain in s for s in self.sites):
            raise SearchConsoleError("no_property", "none")
        return {"domain": domain, "property": self.sites[0], "account": self.who}


@pytest.fixture
def signed_in(api, monkeypatch):
    client, set_model = api
    app.dependency_overrides[require_caller] = lambda: Caller("google", "sam@agency.example.co.uk")
    shared = Fake("connected account owner", ["sc-domain:lawnco.example.co.uk"])
    app.dependency_overrides[get_search] = lambda: shared
    people = {}
    def fake_person(email, token):
        people.setdefault(email, []).append(token)
        return mine
    mine = Fake("your account (sam@agency.example.co.uk)", ["https://www.samsclient.co.uk/"])
    monkeypatch.setattr("app.main.person_console", fake_person)
    return client, mine, shared, people


def test_clients_come_from_your_own_search_console(signed_in):
    client, mine, shared, people = signed_in
    out = client.get("/clients", headers={"X-Google-Access-Token": "ya29.sam"}).json()
    assert [c["domain"] for c in out["clients"]] == ["samsclient.co.uk"]
    assert people == {"sam@agency.example.co.uk": ["ya29.sam"]}


def test_without_a_token_the_shared_account_is_used(signed_in):
    client, *_ = signed_in
    out = client.get("/clients").json()
    assert [c["domain"] for c in out["clients"]] == ["lawnco.example.co.uk"]


def test_the_shared_key_never_uses_a_persons_token(signed_in):
    client, mine, shared, people = signed_in
    app.dependency_overrides[require_caller] = lambda: Caller("api_key")
    out = client.get("/clients", headers={"X-Google-Access-Token": "ya29.someone"}).json()
    assert [c["domain"] for c in out["clients"]] == ["lawnco.example.co.uk"] and people == {}


def test_falls_back_to_the_shared_account():
    shared = Fake("shared", ["sc-domain:lawnco.example.co.uk"])
    # their own sign-in can't read Search Console at all
    assert PersonFirst(Fake("me", fail="no_access"), shared).properties()[0]["siteUrl"] == "sc-domain:lawnco.example.co.uk"
    # their account has no property for this client, the shared one does
    mine = Fake("me", ["https://www.samsclient.co.uk/"])
    assert PersonFirst(mine, shared).report("lawnco.example.co.uk")["account"] == "shared"
    assert PersonFirst(mine, shared).report("samsclient.co.uk")["account"] == "me"
    with pytest.raises(SearchConsoleError):
        PersonFirst(Fake("me", fail="no_access"), None).properties()


def test_each_person_gets_their_own_cache_and_the_newest_token():
    gsc._people.clear()
    a1 = person_console("a@agency.example.co.uk", "t1")
    a2 = person_console("a@agency.example.co.uk", "t2")
    b = person_console("b@agency.example.co.uk", "t3")
    assert a1 is a2 and a1.session.token == "t2"
    assert b is not a1 and b.session.token == "t3"
    assert isinstance(a1.session, TokenSession) and "a@agency.example.co.uk" in a1.who
    gsc._people.clear()


def test_the_token_is_sent_to_google_only_as_a_bearer_header(monkeypatch):
    seen = {}
    import requests
    def fake(method, url, headers=None, timeout=None, **kw):
        seen.update(method=method, url=url, headers=headers)
        class R: status_code = 200; text = "{}"; json = staticmethod(lambda: {"siteEntry": []})
        return R()
    monkeypatch.setattr(requests, "request", fake)
    gsc.SearchConsole(session=TokenSession("ya29.abc"), who="me").properties()
    assert seen["headers"]["Authorization"] == "Bearer ya29.abc" and seen["url"].startswith("https://www.googleapis.com/")
