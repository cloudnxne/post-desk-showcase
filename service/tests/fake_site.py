"""A pretend client website served through httpx.MockTransport, so crawler tests never touch the network.
Page URLs are deliberately opaque (/p/101...) so only the content says what each page is about."""
import httpx

BASE = "https://www.example-landscapes.co.uk"


def page(title, h1, meta, body, extra_head="", nav=True):
    nav_html = "<nav><a href='/'>Home</a> Garden design Patio Fencing Decking Contact us menu</nav>" if nav else ""
    return f"""<!doctype html><html><head><title>{title}</title>
<meta name="description" content="{meta}">{extra_head}
<script>var tracking = "garden patio fencing";</script><style>.x{{}}</style></head>
<body>{nav_html}<header>Call 01234 567890</header>
<main><h1>{h1}</h1><p>{body}</p></main>
<footer>© Example Landscapes. Garden design, patios, fencing, decking.</footer></body></html>"""


PAGES = {
    "/p/101": page("Garden Design Service | Example Landscapes", "Garden design",
                   "Bespoke garden design: we plan planting beds, borders, paths and seating areas.",
                   "Our garden designers plan planting schemes, flower beds, borders and seating areas so your garden layout works all year. " * 3),
    "/p/102": page("Patio Installation | Example Landscapes", "Patio installation",
                   "Natural stone and porcelain patios laid on proper groundwork.",
                   "We lay natural stone, sandstone and porcelain patio slabs on a compacted sub-base, with pointing and drainage. " * 3),
    "/p/103": page("Fencing | Example Landscapes", "Fencing",
                   "Closeboard and panel fencing, gates and posts.",
                   "Closeboard fencing, featheredge panels, concrete posts, gravel boards and garden gates installed. " * 3),
    "/p/104": page("Composite Decking | Example Landscapes", "Composite decking",
                   "Low maintenance composite decking that won't rot or splinter.",
                   "Composite decking boards, joists and balustrades fitted on a level frame for a low maintenance deck. " * 3),
    "/p/old": page("Old page", "Old page", "Hidden", "This page is not for search engines.", '<meta name="robots" content="noindex, follow">'),
    "/p/private": page("Staff", "Staff", "Staff only", "Staff rota.", nav=False),
}

ROBOTS = "User-agent: *\nDisallow: /p/private\n"

import gzip as _gzip

SITEMAP_INDEX = f"""<?xml version="1.0"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<sitemap><loc>{"https://www.example-landscapes.co.uk"}/sitemap-pages.xml</loc></sitemap>
<sitemap><loc>{"https://www.example-landscapes.co.uk"}/sitemap-more.xml.gz</loc></sitemap>
<sitemap><loc>{"https://www.example-landscapes.co.uk"}/sitemap-broken.xml</loc></sitemap>
<sitemap><loc>https://competitor.com/sitemap.xml</loc></sitemap></sitemapindex>"""
SITEMAP_PAGES = """<urlset><url><loc>https://www.example-landscapes.co.uk/p/101</loc></url>
<url><loc>https://www.example-landscapes.co.uk/p/102</loc></url></urlset>"""
SITEMAP_MORE = _gzip.compress(b"""<urlset><url><loc>https://www.example-landscapes.co.uk/p/103</loc></url>
<url><loc>https://www.example-landscapes.co.uk/p/104</loc></url><url><loc>https://www.example-landscapes.co.uk/p/101</loc></url></urlset>""")


def handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if request.url.host == "www.example-landscapes.co.uk":
        if path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS, headers={"content-type": "text/plain"})
        if path == "/sitemap.xml":
            return httpx.Response(200, text=SITEMAP_INDEX, headers={"content-type": "application/xml"})
        if path == "/sitemap-pages.xml":
            return httpx.Response(200, text=SITEMAP_PAGES, headers={"content-type": "text/xml"})
        if path == "/sitemap-more.xml.gz":
            return httpx.Response(200, content=SITEMAP_MORE, headers={"content-type": "application/x-gzip"})
        if path == "/moved":
            return httpx.Response(301, headers={"location": "/p/101"})
        if path == "/escape":
            return httpx.Response(302, headers={"location": "http://169.254.169.254/computeMetadata/v1/"})
        if path == "/elsewhere":
            return httpx.Response(302, headers={"location": "https://competitor.com/"})
        if path == "/brochure.pdf":
            return httpx.Response(200, content=b"%PDF-1.4", headers={"content-type": "application/pdf"})
        if path == "/huge":
            return httpx.Response(200, content=b"<html>" + b"x" * 2_100_000, headers={"content-type": "text/html"})
        if path in PAGES:
            return httpx.Response(200, text=PAGES[path], headers={"content-type": "text/html; charset=utf-8"})
    return httpx.Response(404, text="<html><title>Not found</title></html>", headers={"content-type": "text/html"})


def transport(counter: dict | None = None):
    def h(request):
        if counter is not None:
            counter[str(request.url)] = counter.get(str(request.url), 0) + 1
        return handler(request)
    return httpx.MockTransport(h)


def public_dns(host):
    """Everything resolves to a public address, except the obvious internal ones."""
    return {"169.254.169.254": ["169.254.169.254"], "localhost": ["127.0.0.1"], "internal.example-landscapes.co.uk": ["10.0.0.5"]}.get(host, ["93.184.216.34"])
