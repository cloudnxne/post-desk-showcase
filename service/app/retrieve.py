"""Finds the client's pages that are closest in meaning to a new post.

The post is split into chunks of a few paragraphs and each chunk is embedded, so a page that matches one
section strongly (say, the patio paragraph) ranks well even if the rest of the post is about something
else. A page's score is its best match across the chunks. This also spreads link candidates across the post.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

import numpy as np

from .embed import QUERY, Embedder
from .store import Index

CHUNK_CHARS = 1200
MAX_CHUNKS = 12

# Pages the crawler found that should never be linked to.
UNLINKABLE = {"noindex", "http_404", "http_410", "other_domain"}


def chunk_post(plain: str, title: str = "", keyword: str = "") -> list[str]:
    chunks, cur = [], ""
    for para in (p.strip() for p in plain.split("\n")):
        if not para:
            continue
        if cur and len(cur) + len(para) + 1 > CHUNK_CHARS:
            chunks.append(cur)
            cur = ""
        cur = f"{cur}\n{para}" if cur else para
    if cur:
        chunks.append(cur)
    head = " ".join(x for x in (title, keyword) if x).strip()
    if len(chunks) > MAX_CHUNKS:   # keep the first and last parts and an even spread between
        idx = np.linspace(0, len(chunks) - 1, MAX_CHUNKS).round().astype(int)
        chunks = [chunks[i] for i in sorted(set(idx))]
    return ([head] if head else []) + [c[:CHUNK_CHARS * 2] for c in chunks]


def rank_by_meaning(index: Index, urls: list[str], plain: str, title: str, keyword: str, embedder: Embedder) -> list[tuple[str, float]]:
    """Indexed URLs from `urls`, best match first, with their scores (cosine similarity, -1 to 1)."""
    indexed = [u for u in urls if u in index.vectors]
    chunks = chunk_post(plain, title, keyword)
    if not indexed or not chunks:
        return []
    q = embedder.embed(chunks, QUERY)              # chunks x dims
    scores = (index.matrix(indexed) @ q.T).max(axis=1)
    order = np.argsort(-scores, kind="stable")
    return [(indexed[i], float(scores[i])) for i in order]


def unlinkable(index: Index | None, url: str) -> bool:
    p = index.pages.get(url) if index else None
    return bool(p and p.get("status") == "skipped" and p.get("reason") in UNLINKABLE)


CONTACT = "contact or quote page"
ARTICLE = "article"
MAIN_SERVICE = "main service page"
SERVICE = "service or product page"
OTHER = "other page (about, reviews, policies)"
_CONTACT_RE = re.compile(r"contact|quote|estimate|enquir|get-in-touch|book(ing)?(-|/|$)|free-survey|request-a|callback|call-back")
_ARTICLE_RE = re.compile(r"/(blog|news|latest-news|articles?|guides?|insights|tips|advice|resources|learn|knowledge|faqs?)/")
# Pages that are never money pages. Matched against the last part of the path, so /areas-covered/ is "other"
# but a location page under it (/areas-covered/maidstone/) is still a service page.
_OTHER_LAST = re.compile(r"^(about(-us)?|who-we-are|our-story|privacy(-policy)?|cookies?(-policy)?|terms.*|.*-policy|legal|"
                         r"careers|jobs|vacancies|reviews|testimonials|our-work|gallery|portfolio|case-studies|projects|"
                         r"(meet-)?(the-|our-)?team|areas?-(covered|we-cover|served)|locations|sitemap|accessibility|"
                         r"login|my-account|account|cart|basket|checkout|thank-?you|trade|samples?|brochures?|downloads?)$")
_OTHER_ANY = re.compile(r"/(our-work|gallery|portfolio|case-stud(y|ies)|projects|testimonials|reviews)/")
# Hubs that hold the main services one level down, e.g. /services/patios/.
_SERVICE_HUBS = {"services", "our-services", "what-we-do", "products", "solutions"}


def page_kind(url: str, page: dict | None = None) -> str:
    """A rough label from the URL path (and title for contact pages), so Claude can tell money pages from the rest.
    Main service pages sit at the top level of the site (/artificial-grass/), or one level under a services hub."""
    path = urlsplit(url).path.lower()
    title = ((page or {}).get("title") or "").lower()
    if _CONTACT_RE.search(path) or re.search(r"\b(contact us|get a quote|free quote|request a quote)\b", title):
        return CONTACT
    if _ARTICLE_RE.search(path):
        return ARTICLE
    parts = [p for p in path.split("/") if p]
    if parts and (_OTHER_LAST.match(re.sub(r"\.(html?|php|aspx?)$", "", parts[-1])) or _OTHER_ANY.search(path + "/")):
        return OTHER
    if len(parts) == 1 or (len(parts) == 2 and parts[0] in _SERVICE_HUBS):
        return MAIN_SERVICE
    return SERVICE


# Lower comes first when choosing which links to keep.
KIND_RANK = {MAIN_SERVICE: 1, SERVICE: 2, CONTACT: 2, ARTICLE: 3, OTHER: 4}
