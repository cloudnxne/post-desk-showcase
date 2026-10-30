"""The copy page: the side panel saves the post's HTML and opens a page with a working Copy button."""
import json
import time

import pytest

from app import copy_page
from app.main import app, get_store
from app.store import LocalStore


@pytest.fixture
def client(api, tmp_path):
    c, _ = api
    store = LocalStore(tmp_path)
    app.dependency_overrides[get_store] = lambda: store
    return c, store


def test_saves_and_shows_the_html_with_a_copy_button(client):
    c, _ = client
    r = c.post("/copy", json={"html": "<h2>Steps</h2>\n<p>Lay the <a href=\"https://x/\">sub-base</a>.</p>", "title": "Patio sub-base"})
    assert r.status_code == 200, r.text
    url = r.json()["url"]
    assert url.startswith("https://") and "/copy/" in url
    page = c.get("/copy/" + r.json()["id"])
    assert page.status_code == 200
    assert "&lt;h2&gt;Steps&lt;/h2&gt;" in page.text and "<h2>Steps</h2>" not in page.text     # shown as code, not run
    assert "Copy HTML" in page.text and "Patio sub-base" in page.text
    csp = page.headers["content-security-policy"]
    nonce = csp.split("'nonce-")[1].split("'")[0]
    assert f'<script nonce="{nonce}">' in page.text and "default-src 'none'" in csp
    assert page.headers["x-robots-tag"] == "noindex"


def test_a_script_in_the_post_is_never_run(client):
    c, _ = client
    r = c.post("/copy", json={"html": "</textarea><script>alert(1)</script>", "title": "<b>x</b>"})
    page = c.get("/copy/" + r.json()["id"]).text
    assert "<script>alert(1)</script>" not in page and "&lt;/textarea&gt;&lt;script&gt;" in page
    assert "<b>x</b>" not in page


def test_saving_needs_sign_in(client):
    c, _ = client
    assert c.post("/copy", json={"html": "<p>x</p>"}, headers={"X-API-Key": "wrong"}).status_code == 401


def test_unknown_bad_or_expired_links_say_so(client):
    c, store = client
    assert c.get("/copy/" + "a" * 24).status_code == 404
    assert c.get("/copy/../../etc").status_code == 404
    old = "b" * 24
    store.put_text(copy_page.key(old), json.dumps({"html": "<p>x</p>", "title": "", "created": time.time() - 8 * 86400}))
    r = c.get("/copy/" + old)
    assert r.status_code == 404 and "expired" in r.text
