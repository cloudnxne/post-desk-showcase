"""Post Desk core logic in Python: a port of the generate-side functions in src/core.js.

The page and this service must agree on what counts as a valid anchor, so tests/test_parity.py
checks this module against golden cases produced by the JavaScript version.
"""
from __future__ import annotations

import math
import re
from html import unescape
from html.parser import HTMLParser
from urllib.parse import unquote, urlsplit

STOP = set(
    "the a an and or but of to in on for with at by from as is are was were be been it its this that these those you your we our they "
    "their he she his her i me my not no yes can will would should could how what why when where which who about into over under more "
    "most less than then so if do does did have has had just also very all any each some such only own same too out up down off again "
    "further once here there both few other new page html www com blog category tag".split()
)

META_MIN, META_MAX, TITLE_MAX = 120, 160, 60

_WORD = re.compile(r"[a-z0-9]")


def fold(s: str) -> str:
    """Curly quotes and long dashes to plain ones, then lower case, so matching ignores typography."""
    s = re.sub("[‘’ʼ]", "'", str(s))
    s = re.sub("[“”]", '"', s)
    s = re.sub("[–—]", "-", s)
    return s.lower()


def tokens(s: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9]+", str(s).lower()) if len(w) > 2 and w not in STOP]


def slug_of(u: str) -> str:
    parts = urlsplit(u)
    if not parts.scheme or not parts.netloc:
        return u
    path = parts.path or "/"
    if path == "/":
        return "(home page)"
    try:
        return unquote(path, errors="strict")
    except UnicodeDecodeError:
        return u


def norm_url(u: str | None) -> str:
    u = (u or "").strip()
    u = re.sub(r"[#?].*$", "", u, flags=re.S)
    u = re.sub(r"/$", "", u)
    return u.lower()


def meta_status(n: int) -> str:
    return "short" if n < META_MIN else "good" if n <= META_MAX else "may truncate"


def title_status(n: int) -> str:
    return "good" if n <= TITLE_MAX else "may truncate"


# ---------- sitemap ----------
_LOC = re.compile(r"<loc>\s*(?:<!\[CDATA\[)?\s*([^<\]]+?)\s*(?:\]\]>)?\s*</loc>", re.I)
_SKIP = re.compile(r"\.(xml|jpg|jpeg|png|gif|webp|pdf)(\?|$)", re.I)


def parse_sitemap(text: str) -> dict:
    locs = [m.strip() for m in _LOC.findall(text)]
    raw = locs if locs else [s for s in re.split(r"[\s,]+", text) if re.match(r"https?://", s, re.I)]
    is_index = bool(re.search(r"<sitemapindex", text, re.I))
    urls = [u for u in dict.fromkeys(u.replace("&amp;", "&") for u in raw) if not _SKIP.search(u)]
    return {"urls": urls, "is_index": is_index, "child_sitemaps": locs if is_index else []}


def rank_urls(urls: list[str], content: str, keyword: str, limit: int) -> list[str]:
    """Ranks sitemap URLs by how many of the post's words appear in the URL slug (step 3 replaces this with embeddings)."""
    if len(urls) <= limit:
        return list(urls)
    freq: dict[str, int] = {}
    for w in tokens(f"{content} {keyword} {keyword}"):
        freq[w] = freq.get(w, 0) + 1
    scored = []
    for u in urls:
        s = 0.0
        for t in tokens(slug_of(u)):
            if t in freq:
                s += 1 + math.log(freq[t])
            elif len(t) >= 5:
                for w, f in freq.items():
                    if len(w) >= 5 and w.startswith(t[:5]):
                        s += 0.5 + math.log(f) / 2
                        break
        depth = slug_of(u).count("/")
        scored.append((u, s - depth * 0.05))
    scored.sort(key=lambda x: -x[1])  # stable, like Array.prototype.sort
    return [u for u, _ in scored[:limit]]


# ---------- post text ----------
class _TextAndHeadings(HTMLParser):
    """Collects all text (like DOM textContent) and the text of each h1 to h6."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.text: list[str] = []
        self.headings: list[str] = []
        self._depth = 0
        self._buf: list[str] = []

    def handle_starttag(self, tag, attrs):
        if re.fullmatch(r"h[1-6]", tag):
            if self._depth == 0:
                self._buf = []
            self._depth += 1

    def handle_endtag(self, tag):
        if re.fullmatch(r"h[1-6]", tag) and self._depth:
            self._depth -= 1
            if self._depth == 0:
                self.headings.append("".join(self._buf))

    def handle_data(self, data):
        self.text.append(data)
        if self._depth:
            self._buf.append(data)


def _tidy(t: str) -> str:
    return re.sub(r"[ \t ]+", " ", t).strip()


def post_plain(content: str) -> tuple[str, set[str]]:
    """Plain text of the post, one block per line, plus the folded heading lines anchors must avoid."""
    is_html = bool(re.search(r"<[a-z][\s\S]*>", content, re.I))
    if is_html:
        p = _TextAndHeadings()
        p.feed(re.sub(r"</(p|h[1-6]|li|div)>", lambda m: m.group(0) + "\n", content, flags=re.I))
        p.close()
        text = "".join(p.text)
        headings = {fold(_tidy(h)) for h in p.headings}
    else:
        text = content
        headings = {fold(_tidy(re.sub(r"^\s*#{1,6}\s+", "", line))) for line in content.split("\n") if re.match(r"^\s*#{1,6}\s", line)}
    plain = re.sub(r"^#{1,6}\s+", "", text, flags=re.M)
    plain = re.sub(r"[ \t ]+", " ", plain)
    plain = re.sub(r"\s*\n\s*", "\n", plain).strip()
    return plain, headings


# ---------- anchor checks ----------
_EDGE = re.compile(r"^[\"'“‘]+|[\"'”’.,;:!?]+$")
_SENTENCE = re.compile(r"[.!?][\"')\]]?\s+(?=[A-Z0-9\"'“])")


def locate_anchor(plain: str, anchor: str | None, headings: set[str] | None = None) -> dict | None:
    """Returns {anchor, sentence} in the post's own wording, or None when the phrase isn't in the body text."""
    headings = headings or set()
    a = fold(_EDGE.sub("", re.sub(r"\s+", " ", str(anchor or "")).strip()))
    if len(a) < 3:
        return None
    hay = fold(plain.replace("\n", " "))
    i, start = -1, 0
    while True:
        i = hay.find(a, start)
        if i < 0:
            break
        before = hay[i - 1] if i > 0 else ""
        after = hay[i + len(a)] if i + len(a) < len(hay) else ""
        whole = not (before and _WORD.match(before)) and not (after and _WORD.match(after))
        ls = plain.rfind("\n", 0, i) + 1
        le = plain.find("\n", i)
        in_heading = fold(plain[ls: le if le >= 0 else len(plain)]) in headings
        if whole and not in_heading:
            break  # whole words, body text only (never a heading)
        start = i + 1
    if i < 0:
        return None
    flat = plain.replace("\n", " ")
    exact = flat[i: i + len(a)]
    line_start = plain.rfind("\n", 0, i) + 1
    line_end = plain.find("\n", i)
    line_end = len(plain) if line_end < 0 else line_end
    line, rel = flat[line_start:line_end], i - line_start
    starts = [0] + [m.end() for m in _SENTENCE.finditer(line)]
    s = max(x for x in starts if x <= rel)
    nexts = [x for x in starts if x > rel + len(a) - 1]
    e = min(nexts) if nexts else len(line)
    return {"anchor": exact, "sentence": line[s:e].strip()}


def check_links(raw: list, plain: str, known: set[str], taken: dict, headings: set[str] | None = None) -> tuple[list[dict], list[dict]]:
    """Splits Claude's link suggestions into verified links and ones whose anchor needs a retry.
    Links to URLs outside the sitemap, or to a URL already used, are dropped outright."""
    ok, bad = [], []
    for link in raw or []:
        if not isinstance(link, dict) or not link.get("target_url") or not link.get("anchor_text"):
            continue
        t = norm_url(link["target_url"])
        if t not in known or t in taken["urls"]:
            continue
        hit = locate_anchor(plain, link["anchor_text"], headings)
        if not hit or fold(hit["anchor"]) in taken["anchors"]:
            bad.append(link)
            continue
        taken["urls"].add(t)
        taken["anchors"].add(fold(hit["anchor"]))
        ok.append({**link, "anchor_text": hit["anchor"], "sentence": hit["sentence"], "verified": True})
    return ok, bad
