"""The two prompts Post Desk sends to the model. Kept word for word in line with src/page.html."""
from __future__ import annotations

MAX_CONTENT = 60000


def _url_section(candidates: list[str], total_urls: int, pages: dict[str, dict] | None, priority: list[str] = ()) -> str:
    if pages is None:
        shown = f"{len(candidates)} shown" + (f" of {total_urls}, pre-filtered by relevance" if total_urls > len(candidates) else "")
        return f"EXISTING SITE URLS (from the sitemap; {shown}):\n" + chr(10).join(candidates)
    lines = []
    for u in candidates:
        p = pages.get(u)
        if not p:
            lines.append(u)
            continue
        kind = f"\n  Type: {p['kind']}" if p.get("kind") else ""
        if u in priority:
            kind += ", PRIORITY PAGE" if kind else "\n  Type: PRIORITY PAGE"
        line = f"{u}{kind}\n  Title: {p.get('title') or '(none)'}\n  About: {p.get('summary') or '(no description)'}"
        if p.get("search"):
            line += "\n  Google: " + search_line(p["search"])
        lines.append(line)
    return (f"EXISTING SITE PAGES (from the sitemap; {len(candidates)} shown of {total_urls}, chosen by how closely each page's "
            f"content matches this post; title and summary given where the page has been read):\n" + chr(10).join(lines))


# Extra rules for the API's semantic path, where Claude can see what kind of page each one is.
LINK_MIX_RULES = """
   - When more than one page fits a phrase, choose in this order: the PRIORITY PAGES above, then main service pages, then other service or product pages, then articles. Give priority pages and main service pages priority "high". Aim for at least one priority or main service page when a relevant one is listed.
   - Don't link an "other page" (about, reviews, policies) unless the post is really about it.
   - If the post has a call to action (a free quote, a survey, getting in touch, booking), link that phrase to the contact or quote page when one is listed. This is as well as the priority or main service link, not instead of it.
   - The anchor must name the topic of the page it links to, so a reader knows where it goes. Avoid generic phrases that could describe many pages (for example "low maintenance", "find out more", "this guide", "our team")."""


# Added on the semantic path when the client has priority or main service pages in the list.
MUST_LINK_RULE = """
   - Priority and main service pages here: {urls}. If any phrase in the post names one of these services, even just the service itself (such as the product or job the page sells), at least one of your links must go to one of them. Naming a page's own service is not a generic phrase."""


# Added when Search Console data marks some candidates as close to Google's first page.
SEARCH_RULES = """
   - Pages marked "CLOSE TO PAGE ONE" rank just off Google's first page for searches people make, so a new internal link helps them. Use this to choose between pages of the same type: never pick an article over a relevant priority or main service page because of it. A priority or service page that is also close to page one is the strongest pick of all.
   - Where a page's top searches (or close wording) already appear in the post, prefer that wording as the anchor."""


def search_line(s: dict) -> str:
    out = f"average position {s['position']}, {s['impressions']:,} impressions"
    if s.get("close_to_page_one"):
        out += ", CLOSE TO PAGE ONE"
    if s.get("top_searches"):
        out += "; top searches: " + ", ".join(f'"{q}"' for q in s["top_searches"])
    return out


def link_range(words: int) -> tuple[int, int]:
    """Roughly one link per 100 to 150 words: (min, max) for a post of this many words. 1,000+ words gives 5 to 10."""
    hi = min(10, max(2, -(-words // 100)))
    lo = min(hi, max(1, words // 200))
    return lo, hi


def generate_prompt(client: dict, post: dict, candidates: list[str], total_urls: int, priority: list[str],
                    pages: dict[str, dict] | None = None, links: tuple[int, int] = (4, 10)) -> str:
    """With pages=None and the default link range this is word for word the page's prompt (checked by the parity
    tests). With pages (from the semantic index) Claude also sees each page's type, title and summary, and gets
    rules on service pages, calls to action and anchors. `links` sets how many links to ask for."""
    n_links = f"{links[0]}–{links[1]}" if links[0] < links[1] else str(links[1])
    mix_rules = LINK_MIX_RULES if pages is not None else ""
    main = [u for u in candidates if (pages or {}).get(u, {}).get("kind") == "main service page"]
    must = [u for u in priority if u in candidates] + main
    if pages is not None and must:
        mix_rules += MUST_LINK_RULE.format(urls=", ".join(dict.fromkeys(must)))
    if pages and any(p.get("search", {}).get("close_to_page_one") for p in pages.values()):
        mix_rules += SEARCH_RULES
    content = post["content"]
    if len(content) > MAX_CONTENT:
        content = content[:MAX_CONTENT] + "\n[…truncated]"
    relevance = ("Judge relevance from the URL path." if pages is None
                 else "Judge relevance from each page's title and summary, or from its URL path where none is given.")
    return f"""You are an SEO editor preparing a blog post for publishing on a client's website.

CLIENT: {client["name"]} ({client.get("domain") or "domain not given"})
CLIENT NOTES (audience, tone, rules): {client.get("notes") or "none"}
PRIORITY PAGES (link to these when genuinely relevant, e.g. service/money pages):
{chr(10).join(priority) or "none"}

POST TITLE: {post.get("title") or "(not given — infer from content)"}
TARGET KEYWORD: {post.get("keyword") or "(not given — infer the main topic)"}
POST URL: {post.get("url") or "(not given)"}

{_url_section(candidates, total_urls, pages, priority)}

POST CONTENT:
\"\"\"
{content}
\"\"\"

TASKS
1. Write 3 meta descriptions, each 140–158 characters, each a different angle (e.g. benefit-led, question-led, keyword-first). Include the target keyword naturally, write in the client's voice, end with a reason to click. No quotation marks, no clickbait.
2. Suggest an SEO title tag (max 60 characters).
3. Find {n_links} internal linking opportunities FROM this post TO existing URLs above. Rules:
   - Work from the post outward: find phrases ALREADY WRITTEN in the post that describe what an existing page is about, then link that phrase. Never invent, reword, pluralise, re-case or shorten-with-ellipsis an anchor. Do not suggest adding new text.
   - anchor_text MUST be copied character for character from the post content (2–6 words, natural and descriptive; never "click here", "read more" or a lone generic word). Prefer the phrase's first appearance in body text. Never use a phrase that only appears in a heading.
   - sentence MUST be the exact sentence from the post that contains the anchor.
   - If no phrase in the post fits a page, skip that page. Fewer accurate links beat more forced ones.
   - Only use URLs from the list. {relevance} Each target URL once, each anchor once. Spread links across the post. Don't link the home page unless it truly fits.
   - priority: "high" for service/priority pages or very close topical matches, otherwise "medium" or "low".{mix_rules}
4. Suggest 3–6 existing URLs from the list that should link BACK to this new post, with a suggested anchor text.
5. HEADINGS for SEO-friendly HTML. List every line of the post that works as a heading (the post title line if present, section headings, sub-headings, FAQ questions), with its text copied exactly as written in the post. Levels: 1 = the post's main title only; 2 = main sections; 3 = sub-sections or questions inside an H2 section; 4 only if truly nested deeper. Never skip a level. Normal sentences and paragraphs are not headings.
6. LISTS for SEO-friendly HTML. Bullet symbols are often lost when a post is pasted, so find every run of lines that are really list items (short parallel lines, steps, features, pros/cons, often after a lead-in line ending in a colon). Copy each item exactly as written. type "ol" for ordered steps or rankings, otherwise "ul". Don't include the lead-in line, headings or full paragraphs.

Return ONLY JSON in this shape:
{{"title_tag":"...","primary_keyword":"...","meta_descriptions":[{{"text":"...","angle":"..."}}],"internal_links":[{{"anchor_text":"...","sentence":"...","target_url":"...","reason":"...","priority":"high|medium|low"}}],"link_back_from":[{{"source_url":"...","anchor_text":"...","reason":"..."}}],"headings":[{{"text":"...","level":2}}],"lists":[{{"type":"ul","items":["..."]}}]}}"""


def retry_prompt(bad: list[dict], used_anchors: list[str], plain: str) -> str:
    fixes = "\n".join(f'- {b["target_url"]} (rejected anchor: "{b["anchor_text"]}")' for b in bad)
    return f"""These internal link suggestions for a blog post used anchor text that does NOT appear word for word in the post (or reused an anchor already taken). For each target URL, find a different phrase that is ALREADY in the post, copied character for character (2–6 words, descriptive), that fits that page. If none fits, leave that URL out.

Anchors already used (don't reuse): {" | ".join(used_anchors) or "none"}

Links to fix:
{fixes}

POST CONTENT:
\"\"\"
{plain[:MAX_CONTENT]}
\"\"\"

Return ONLY JSON: {{"internal_links":[{{"anchor_text":"...","sentence":"...","target_url":"...","reason":"...","priority":"high|medium|low"}}]}}"""


def main_link_prompt(targets: list[tuple[str, dict]], used_anchors: list[str], plain: str) -> str:
    """A short follow-up when the first answer linked none of the client's most important pages."""
    pages = "\n".join(f"{u}\n  Title: {p.get('title') or '(none)'}\n  About: {p.get('summary') or '(no description)'}" for u, p in targets)
    return f"""This blog post needs one internal link to one of the client's most important pages (their priority or main service pages):

{pages}

Find a phrase that is ALREADY in the post, copied character for character (2–6 words), that names what one of these pages offers, and link it to that page. Naming the service itself counts (for example the product or job the page sells). Pick the page and phrase that fit best. Never use a phrase that only appears in a heading. If nothing in the post fits any of these pages, return an empty list.

Anchors already used (don't reuse): {" | ".join(used_anchors) or "none"}

POST CONTENT:
\"\"\"
{plain[:MAX_CONTENT]}
\"\"\"

Return ONLY JSON: {{"internal_links":[{{"anchor_text":"...","sentence":"...","target_url":"...","reason":"...","priority":"high"}}]}}"""
