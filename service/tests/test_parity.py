"""The service must agree with the page. golden.json holds what src/core.js (and the page's prompts) give for a set of
inputs; scripts/golden.mjs writes it, and CI fails if it goes stale. Every case here must match exactly."""
import json
from pathlib import Path

import pytest

from app import core
from app.prompts import generate_prompt, retry_prompt

G = json.loads((Path(__file__).parent / "golden.json").read_text(encoding="utf8"))


@pytest.mark.parametrize("name", list(G["postPlain"]))
def test_post_plain(name):
    case = G["postPlain"][name]
    plain, headings = core.post_plain(case["content"])
    assert plain == case["plain"]
    assert sorted(headings) == case["headings"]


@pytest.mark.parametrize("case", G["locateAnchor"], ids=lambda c: f'{c["post"]}:{c["anchor"]!r}')
def test_locate_anchor(case):
    post = G["postPlain"][case["post"]]
    assert core.locate_anchor(post["plain"], case["anchor"], set(post["headings"])) == case["result"]


def test_golden_covers_found_and_missing_anchors():
    results = [c["result"] for c in G["locateAnchor"]]
    assert any(r is None for r in results) and any(r is not None for r in results)


def test_check_links():
    case, post = G["checkLinks"], G["postPlain"]["md"]
    taken = {"urls": set(), "anchors": set()}
    ok, bad = core.check_links(case["raw"], post["plain"], {core.norm_url(u) for u in case["known"]}, taken, set(post["headings"]))
    assert ok == case["ok"]
    assert bad == case["bad"]
    assert {"urls": sorted(taken["urls"]), "anchors": sorted(taken["anchors"])} == case["taken"]


@pytest.mark.parametrize("case", G["slugOf"], ids=lambda c: c["url"])
def test_slug_of(case):
    assert core.slug_of(case["url"]) == case["result"]


@pytest.mark.parametrize("case", G["normUrl"], ids=lambda c: c["url"])
def test_norm_url(case):
    assert core.norm_url(case["url"]) == case["result"]


@pytest.mark.parametrize("case", G["tokens"])
def test_tokens(case):
    assert core.tokens(case["text"]) == case["result"]


def test_parse_sitemap():
    r, e = core.parse_sitemap(G["parseSitemap"]["xml"]), G["parseSitemap"]["result"]
    assert (r["urls"], r["is_index"], r["child_sitemaps"]) == (e["urls"], e["isIndex"], e["childSitemaps"])


@pytest.mark.parametrize("case", G["rankUrls"], ids=lambda c: f'{c["keyword"] or "-"}:{c["limit"]}')
def test_rank_urls(case):
    assert core.rank_urls(case["urls"], case["content"], case["keyword"], case["limit"]) == case["result"]


@pytest.mark.parametrize("case", G["metaStatus"], ids=lambda c: str(c["len"]))
def test_meta_status(case):
    assert core.meta_status(case["len"]) == case["result"]


@pytest.mark.parametrize("key", ["generate", "generateBlank"])
def test_generate_prompt_matches_page(key):
    c = G["prompts"][key]
    assert generate_prompt(c["client"], c["post"], c["candidates"], c["total"], c["priority"]) == c["prompt"]


def test_retry_prompt_matches_page():
    r = G["prompts"]["retry"]
    assert retry_prompt(r["bad"], r["used"], r["plain"]) == r["prompt"]
