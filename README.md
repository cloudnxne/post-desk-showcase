# Post Desk

> A public copy of a tool I built at work. Client content, people's names and my own Cloud setup have been taken out; the examples use made-up clients. To run your own, follow `docs/DEPLOY.md` (the deploy workflow runs by hand once the setup is done).

Writes meta descriptions, finds internal links and formats the HTML for client blog posts. Claude does the writing; the code checks everything Claude suggests before a writer sees it.

It runs in two places:

- **The page** on claude.ai, where writers pick a client, paste a post and tick the links they want.
- **The API** (Python, FastAPI) on Google Cloud Run, which does the same generate step for other tools to call, such as a Google Doc add-on. It can also read every page on a client's site once and pick link candidates by meaning (embeddings from Vertex AI), rather than by words in the URL.

## What it checks

- Every link points to a URL in the client's sitemap. Anything else is dropped.
- Every anchor appears word for word in the post body, never only in a heading. Anchors that fail go back to Claude once for an exact phrase.
- Each URL and each anchor is used once, and a post never links to itself.
- The number of links scales with the post's length (about one per 100 to 150 words); extras are trimmed, lowest priority first.
- The HTML has at most one H1, no skipped heading levels, and lists rebuilt as real `<ul>`/`<ol>`.
- Meta descriptions are flagged under 120 or over 160 characters, title tags over 60.

## Layout

```
src/core.js           page logic: sitemap parsing, URL ranking, anchor checks, HTML building, link insertion
src/page.html         the page (UI, storage, Claude calls), with a marker where core.js goes
scripts/build.mjs     inlines core.js into the page -> dist/index.html
scripts/golden.mjs    runs core.js and the page's prompts on fixed inputs -> service/tests/golden.json
test/                 Vitest tests, including an end-to-end run of the built page

service/app/core.py      Python port of the checks in core.js
service/app/prompts.py   the two prompts, word for word as the page sends them
service/app/generate.py  pick candidates (by meaning when indexed, else URL), ask the model, verify links, retry once
service/app/crawl.py     safe crawler: client's domain only, public IPs only, robots.txt, noindex, size and time caps
service/app/embed.py     Vertex AI embeddings (one text per call, in parallel, with backoff)
service/app/store.py     per-client page index as JSON in Cloud Storage, cached in memory
service/app/indexer.py   incremental indexing: new pages first, refresh after 30 days, re-embed only changed pages
service/app/gsc.py       Search Console: pages close to Google's first page (positions 8 to 20), with top searches
service/app/retrieve.py  post split into sections; a page's score is its best match to any section; page types
                         (main service, service, article, contact, other) so links favour money pages: client
                         priority pages, then main services, then other services, then articles, with Search
                         Console only breaking ties between pages of the same type
service/app/llm.py       model clients behind one interface: Claude (Anthropic API), Gemini and Claude on Vertex AI;
                         Claude by default, Gemini automatically if Claude fails
service/app/auth.py      sign-in: Google accounts in your Workspace domain only, or the shared key
service/app/main.py      FastAPI app: POST /generate, GET /health, JSON logs
service/tests/           pytest, including parity tests against golden.json
apps-script/             the Google Doc front end: a menu and sidebar in one doc (Code.gs, Sidebar.html), and the
                         same as a side-panel add-on for every doc (Card.gs). Setup in docs/DEPLOY.md
service/Dockerfile       image for Cloud Run

.github/workflows/ci.yml      on every push: JS tests, build, golden check, Python tests, Docker build + smoke test
.github/workflows/deploy.yml  tests, build, push to Artifact Registry, deploy to Cloud Run, smoke test the live URL
docs/DEPLOY.md                one-time Google Cloud setup
```

## Keeping the page and the API in step

The page is JavaScript and the API is Python, so the rules could drift apart. To stop that, `scripts/golden.mjs` runs the JavaScript on a fixed set of posts, anchors, URLs and sitemaps, and also fills in the page's two prompt templates. The Python tests must give exactly the same results, prompts included. CI regenerates the golden file on every push and fails if it changed without being committed, so a change on one side forces the same change on the other.

## Commands

```bash
# page
npm install
npm test             # 99 tests
npm run build        # dist/index.html
npm run golden       # refresh service/tests/golden.json after changing core.js or the prompts

# API
cd service
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest     # 385 tests
POST_DESK_API_KEY=dev ANTHROPIC_API_KEY=sk-... .venv/bin/uvicorn app.main:app --reload
# then open http://localhost:8000/docs
```

## API

`POST /generate`, signed in with a Google account in your Workspace domain (`Authorization: Bearer <Google ID token>`), or with the shared key in `X-API-Key` for automations:

```json
{
  "client": {"name": "Example Landscapes", "domain": "example-landscapes.co.uk", "notes": "", "priority_pages": []},
  "post": {"title": "", "keyword": "", "url": "", "content": "the post, as text, Markdown or HTML"},
  "sitemap_urls": ["https://example-landscapes.co.uk/services/garden-design/"]
}
```

Returns the title tag, meta descriptions (with length and status), verified internal links (anchor, sentence, target, reason, priority), pages that should link back, headings, lists, and run stats. Full schema at `/docs`.

`sitemap_urls` can be left empty: the API uses the client's page index, or finds their sitemap itself (robots.txt, then the usual addresses).

`POST /index` with the client's domain and `sitemap_url` reads and embeds the client's pages; `GET /index/{domain}` shows what's indexed and `GET /clients` lists indexed clients plus every Search Console property. `sitemap_url` is optional for `/index` too. See docs/DEPLOY.md.

## Roadmap

1. Tests and CI (done)
2. Python (FastAPI) generate service in Docker on Cloud Run (done, see docs/DEPLOY.md)
3. Crawl each sitemap page and pick links with embeddings instead of URL word matching (done)
4. Model and embedding calls through Vertex AI (done: Gemini and Claude on Vertex, chosen per service or per request)
5. Search Console data to favour pages close to page one (done via the Search Console API; BigQuery export later for reporting)
6. Google Doc front end via Apps Script: sidebar with titles, metas and links, ticked links added to the doc (done)
