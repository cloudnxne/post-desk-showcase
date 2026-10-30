import { describe, it, expect } from "vitest";
import { parseSitemap, normUrl, slugOf, rankUrls, siteFit } from "../src/core.js";
import { SITEMAP_XML } from "./fixtures.js";

describe("parseSitemap", () => {
  const { urls, isIndex } = parseSitemap(SITEMAP_XML);

  it("reads <loc> entries, including CDATA", () => {
    expect(urls).toContain("https://example-landscapes.co.uk/areas/watford/");
    expect(urls).toContain("https://example-landscapes.co.uk/services/patio-installation/");
  });

  it("decodes &amp; in URLs", () => {
    expect(urls).toContain("https://example-landscapes.co.uk/contact/?ref=a&b=c");
  });

  it("removes duplicates", () => {
    expect(urls.filter(u => u.endsWith("/garden-design/"))).toHaveLength(1);
  });

  it("drops images and other non-page files", () => {
    expect(urls.some(u => u.endsWith(".jpg"))).toBe(false);
    expect(isIndex).toBe(false);
  });

  it("spots a sitemap index and lists its child sitemaps", () => {
    const r = parseSitemap(`<sitemapindex><sitemap><loc>https://x.com/post-sitemap.xml</loc></sitemap><sitemap><loc>https://x.com/page-sitemap.xml</loc></sitemap></sitemapindex>`);
    expect(r.isIndex).toBe(true);
    expect(r.childSitemaps).toEqual(["https://x.com/post-sitemap.xml", "https://x.com/page-sitemap.xml"]);
    expect(r.urls).toEqual([]);
  });

  it("falls back to a pasted list of URLs", () => {
    const r = parseSitemap("https://x.com/a\nhttps://x.com/b, not-a-url  https://x.com/a");
    expect(r.urls).toEqual(["https://x.com/a", "https://x.com/b"]);
  });
});

describe("URL helpers", () => {
  it("normUrl ignores case, trailing slash, query and hash", () => {
    expect(normUrl(" https://X.com/Page/?utm=1#top ")).toBe("https://x.com/page");
    expect(normUrl(undefined)).toBe("");
  });

  it("slugOf gives the path, or a label for the home page", () => {
    expect(slugOf("https://x.com/")).toBe("(home page)");
    expect(slugOf("https://x.com/services/patio%20laying/")).toBe("/services/patio laying/");
    expect(slugOf("not a url")).toBe("not a url");
  });
});

describe("rankUrls", () => {
  const urls = Array.from({ length: 40 }, (_, i) => `https://x.com/misc/page-${i}/`)
    .concat(["https://x.com/services/patio-installation/", "https://x.com/services/garden-design/"]);

  it("returns every URL when under the limit", () => {
    expect(rankUrls(urls.slice(0, 3), "anything", "", 10)).toEqual(urls.slice(0, 3));
  });

  it("puts pages whose slug matches the post at the top", () => {
    const top = rankUrls(urls, "Patio installation tips for your patio and garden", "patio", 2);
    expect(top[0]).toBe("https://x.com/services/patio-installation/");
    expect(top).toContain("https://x.com/services/garden-design/");
  });
});

describe("siteFit", () => {
  it("scores a post higher against the client it belongs to", () => {
    const post = "Patio installation and garden design. A patio needs a garden plan.";
    const mine = siteFit(["https://a.com/patio-installation/", "https://a.com/garden-design/"], "a.com", post);
    const other = siteFit(["https://b.com/mobile-vet/", "https://b.com/cat-vaccinations/"], "b.com", post);
    expect(mine).toBeGreaterThan(other);
    expect(other).toBe(0);
  });
});
