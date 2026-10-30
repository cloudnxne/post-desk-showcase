import asyncio

import pytest

from app import crawl
from app.crawl import Blocked, check_url, extract, normalise_domain, summary
from fake_site import BASE, PAGES, public_dns, transport

DOMAIN = "example-landscapes.co.uk"


@pytest.fixture(autouse=True)
def dns(monkeypatch):
    monkeypatch.setattr(crawl, "resolve", public_dns)


def run(urls, counter=None):
    return {r.url: r for r in asyncio.run(crawl.crawl(urls, DOMAIN, transport=transport(counter))).results}


# ---------- what a page is about ----------
def test_extract_reads_title_h1_meta_and_main_text():
    p = extract(PAGES["/p/102"])
    assert p["title"] == "Patio Installation | Example Landscapes"
    assert p["h1"] == "Patio installation"
    assert p["meta"].startswith("Natural stone and porcelain")
    assert "compacted sub-base" in p["text"]


def test_extract_ignores_nav_header_footer_scripts_and_styles():
    text = extract(PAGES["/p/103"])["text"]
    assert "menu" not in text and "01234" not in text and "©" not in text and "tracking" not in text


def test_extract_falls_back_to_body_text_without_a_main_element():
    p = extract("<html><title>T</title><body><div>" + "Useful words here. " * 20 + "</div><footer>skip me</footer></body></html>")
    assert "Useful words" in p["text"] and "skip me" not in p["text"]


def test_extract_uses_og_description_when_there_is_no_meta_description():
    p = extract('<html><head><meta property="og:description" content="From OG"></head><body>x</body></html>')
    assert p["meta"] == "From OG"


def test_extract_spots_noindex():
    assert extract(PAGES["/p/old"])["noindex"] is True
    assert extract(PAGES["/p/101"])["noindex"] is False


def test_extract_survives_broken_html():
    p = extract("<html><title>Broken<body><h1>Head<p>text <b>unclosed <div>more")
    assert p["title"] and "text" in (p["text"] + p["title"])


def test_summary_prefers_meta_and_cuts_on_a_word():
    assert summary({"meta": "Short meta."}) == "Short meta."
    s = summary({"meta": "", "text": "word " * 100}, limit=50)
    assert len(s) <= 51 and s.endswith("…") and not s.endswith(" …")


@pytest.mark.parametrize("raw, expected", [
    ("example-landscapes.co.uk", DOMAIN), ("https://www.Example-Landscapes.co.uk/about", DOMAIN),
    ("www.example-landscapes.co.uk:443", DOMAIN), ("", ""),
])
def test_normalise_domain(raw, expected):
    assert normalise_domain(raw) == expected


# ---------- safety ----------
@pytest.mark.parametrize("url, reason", [
    ("ftp://example-landscapes.co.uk/x", "not_http"),
    ("https://competitor.com/", "other_domain"),
    ("https://evil-example-landscapes.co.uk/", "other_domain"),
    ("https://example-landscapes.co.uk.evil.com/", "other_domain"),
    ("http://169.254.169.254/computeMetadata/v1/", "other_domain"),
    ("https://example-landscapes.co.uk:8080/", "unusual_port"),
])
def test_refuses_urls_outside_the_clients_site(url, reason):
    with pytest.raises(Blocked, match=reason):
        check_url(url, crawl.allowed_hosts(DOMAIN))


def test_refuses_the_clients_own_domain_if_it_points_somewhere_internal():
    hosts = crawl.allowed_hosts(DOMAIN) | {"internal.example-landscapes.co.uk"}
    with pytest.raises(Blocked, match="private_address"):
        check_url("https://internal.example-landscapes.co.uk/", hosts)


def test_refuses_any_private_address_in_the_dns_answer(monkeypatch):
    monkeypatch.setattr(crawl, "resolve", lambda h: ["93.184.216.34", "10.1.2.3"])
    with pytest.raises(Blocked, match="private_address"):
        check_url(BASE + "/", crawl.allowed_hosts(DOMAIN))


def test_redirects_are_checked_at_every_hop():
    got = run([BASE + "/escape", BASE + "/elsewhere", BASE + "/moved"])
    assert got[BASE + "/escape"].reason == "other_domain"        # tried to send us to the metadata server
    assert got[BASE + "/elsewhere"].reason == "other_domain"
    assert got[BASE + "/moved"].ok and got[BASE + "/moved"].page["h1"] == "Garden design"


def test_robots_txt_is_respected_and_fetched_once():
    counter = {}
    got = run([BASE + "/p/private", BASE + "/p/101", BASE + "/p/102"], counter)
    assert got[BASE + "/p/private"].reason == "robots_txt"
    assert counter[BASE + "/robots.txt"] == 1
    assert BASE + "/p/private" not in counter                     # never even requested


def test_skips_with_a_reason():
    got = run([BASE + "/p/old", BASE + "/missing", BASE + "/brochure.pdf", BASE + "/huge"])
    assert {u.rsplit("/", 1)[1]: r.reason for u, r in got.items()} == {
        "old": "noindex", "missing": "http_404", "brochure.pdf": "not_html", "huge": "too_large"}


def test_deadline_stops_early_without_error():
    got = asyncio.run(crawl.crawl([BASE + f"/p/{n}" for n in (101, 102, 103, 104)], DOMAIN, deadline=0, transport=transport()))
    assert got.results == [] and got.stopped_early == ""


# ---------- sitemaps ----------
def test_reads_a_sitemap_index_with_plain_and_gzipped_children():
    urls = asyncio.run(crawl.fetch_sitemap(BASE + "/sitemap.xml", DOMAIN, transport=transport()))
    assert urls == [BASE + f"/p/{n}" for n in (101, 102, 103, 104)]   # broken and off-site children skipped, duplicates removed


def test_reads_a_plain_sitemap():
    assert asyncio.run(crawl.fetch_sitemap(BASE + "/sitemap-pages.xml", DOMAIN, transport=transport())) == [BASE + "/p/101", BASE + "/p/102"]


def test_refuses_a_sitemap_on_another_site():
    with pytest.raises(Blocked, match="other_domain"):
        asyncio.run(crawl.fetch_sitemap("https://competitor.com/sitemap.xml", DOMAIN, transport=transport()))


def test_missing_sitemap_says_so():
    with pytest.raises(Blocked, match="sitemap_http_404"):
        asyncio.run(crawl.fetch_sitemap(BASE + "/nope.xml", DOMAIN, transport=transport()))
