import { describe, it, expect } from "vitest";
import { buildSeoHTML, insertLinks, stripMarker, itemHTML } from "../src/core.js";
import { POST_MD } from "./fixtures.js";

const headingLevels = html => [...html.matchAll(/<h([1-6])>/g)].map(m => +m[1]);

// The rule the page promises: at most one H1, it comes first, and no heading jumps more than one level.
function assertValidOutline(html){
  const levels = headingLevels(html);
  expect(levels.filter(l => l === 1).length).toBeLessThanOrEqual(1);
  if (levels.includes(1)) expect(levels[0]).toBe(1);
  let prev = levels[0] === 1 ? 1 : 1;
  for (const l of levels) { expect(l).toBeLessThanOrEqual(prev + 1); prev = l; }
}

describe("stripMarker", () => {
  it.each([
    ["- Gravel", "ul", "Gravel"],
    ["• Gravel", "ul", "Gravel"],
    ["✓Gravel", "ul", "Gravel"],
    ["o Gravel", "ul", "Gravel"],
    ["1. Survey", "ol", "Survey"],
    ["2) Design", "ol", "Design"],
    ["(3) Build", "ol", "Build"],
    ["b) Build", "ol", "Build"],
    ["Just a sentence.", null, "Just a sentence."],
    ["o is not a bullet", null, "o is not a bullet"],
  ])("%s", (input, type, text) => {
    expect(stripMarker(input)).toEqual({ type, text });
  });
});

describe("itemHTML", () => {
  it("bolds a short label before a colon", () => {
    expect(itemHTML("Artificial grass: stays green")).toBe("<strong>Artificial grass:</strong> stays green");
  });
  it("leaves URLs alone", () => {
    expect(itemHTML("https: //x.com")).toBe("https: //x.com");
  });
});

describe("buildSeoHTML: heading order", () => {
  it("drops the title H1 by default (the CMS adds it)", () => {
    const html = buildSeoHTML(POST_MD, [], "Planning Your Dream Garden", []);
    expect(html).not.toContain("<h1>");
    expect(html.startsWith("<p>A good garden starts")).toBe(true);
  });

  it("keeps one H1 when asked", () => {
    const html = buildSeoHTML(POST_MD, [], "Planning Your Dream Garden", [], { includeH1: true });
    expect(headingLevels(html)[0]).toBe(1);
    assertValidOutline(html);
  });

  it("never skips a level (an H4 under an H2 becomes an H3)", () => {
    const html = buildSeoHTML(POST_MD, [], "Planning Your Dream Garden", []);
    expect(html).toContain("<h3>Sealing your patio</h3>");
    expect(headingLevels(html)).toEqual([2, 3, 2]);
    assertValidOutline(html);
  });

  it("turns a second H1 into an H2", () => {
    const html = buildSeoHTML("# One\n\ntext\n\n# Two\n\nmore", [], "One", [], { includeH1: true });
    expect(headingLevels(html)).toEqual([1, 2]);
  });

  it("adds the title as H1 when the post has none and H1 is on", () => {
    const html = buildSeoHTML("Intro text.\n\n## Section", [], "My Title", [], { includeH1: true });
    expect(html.startsWith("<h1>My Title</h1>")).toBe(true);
  });

  it("uses Claude's heading list for plain lines with no markup", () => {
    const html = buildSeoHTML("Intro text.\nWhy choose us\nBecause we are good.\nIs it expensive?\nNot really.",
      [{ text: "Why choose us", level: 2 }, { text: "Is it expensive?", level: 4 }], "", []);
    expect(html).toContain("<h2>Why choose us</h2>");
    expect(html).toContain("<h3>Is it expensive?</h3>");
    assertValidOutline(html);
  });

  it("produces a valid outline for messy heading levels", () => {
    const md = "#### Deep start\n\ntext\n\n## Mid\n\n###### Very deep\n\n# Late H1";
    for (const includeH1 of [false, true]) assertValidOutline(buildSeoHTML(md, [], "Something else", [], { includeH1 }));
  });
});

describe("buildSeoHTML: lists", () => {
  const html = buildSeoHTML(POST_MD, [], "Planning Your Dream Garden", []);

  it("rebuilds bullet lines as one <ul>", () => {
    expect(html).toContain("<ul>\n  <li><strong>Artificial grass:</strong> stays green all year</li>\n  <li>Gravel borders</li>\n  <li>Raised beds</li>\n</ul>");
  });

  it("rebuilds numbered lines as an <ol>", () => {
    expect(html).toMatch(/<ol>\s*<li>Survey the site<\/li>\s*<li>Agree the design<\/li>\s*<li>Build it<\/li>\s*<\/ol>/);
  });

  it("uses Claude's list hints when bullets were lost in pasting", () => {
    const out = buildSeoHTML("What we offer:\nPatios\nDecking\nFencing\nCall us today.", [], "", [{ type: "ul", items: ["Patios", "Decking", "Fencing"] }]);
    expect(out).toMatch(/<p>What we offer:<\/p>\s*<ul>\s*<li>Patios<\/li>\s*<li>Decking<\/li>\s*<li>Fencing<\/li>\s*<\/ul>\s*<p>Call us today.<\/p>/);
  });
});

describe("buildSeoHTML: HTML input", () => {
  it("strips scripts, inline handlers, styles and classes", () => {
    const out = buildSeoHTML(`<h2 class="x" style="color:red">Title</h2><p onclick="steal()">Hi <script>alert(1)</script>there</p>`, [], "", []);
    expect(out).not.toMatch(/script|onclick|style=|class=/);
    expect(out).toContain("<h2>Title</h2>");
  });

  it("escapes HTML typed into a plain text post", () => {
    expect(buildSeoHTML("Use <b> tags & stuff", [], "", [])).toBe("<p>Use &lt;b&gt; tags &amp; stuff</p>");
  });

  it("copes when the first block is a list and a title is set", () => {
    // Before the refactor this threw, because the first block had no .html to compare with the title.
    expect(() => buildSeoHTML("<ul><li>One</li></ul><p>Text</p>", [], "My Post", [])).not.toThrow();
  });
});

describe("insertLinks: links are placed", () => {
  const html = buildSeoHTML(POST_MD, [], "Planning Your Dream Garden", []);

  it("places every ticked link and counts them", () => {
    const res = insertLinks(html, [
      { anchor_text: "garden design service", target_url: "https://x.com/garden-design/" },
      { anchor_text: "patio installation", target_url: "https://x.com/patio/" },
    ]);
    expect(res.placed).toBe(2);
    expect(res.html).toContain('<a href="https://x.com/garden-design/">garden design service</a>');
    expect(res.html).toContain('<a href="https://x.com/patio/">patio installation</a>');
  });

  it("never links inside a heading", () => {
    const res = insertLinks(html, [{ anchor_text: "Sealing your patio", target_url: "https://x.com/s/" }]);
    expect(res.placed).toBe(0);
    expect(res.html).toBe(html);
  });

  it("links the first body match, keeping the post's casing", () => {
    const res = insertLinks("<h2>Patios</h2><p>Patios are great. More patios here.</p>", [{ anchor_text: "patios", target_url: "/p" }]);
    expect(res.html).toBe('<h2>Patios</h2><p><a href="/p">Patios</a> are great. More patios here.</p>');
  });

  it("matches whole words only", () => {
    expect(insertLinks("<p>A lovely party.</p>", [{ anchor_text: "art", target_url: "/a" }]).placed).toBe(0);
  });

  it("doesn't nest a link inside an existing one", () => {
    const res = insertLinks('<p><a href="/old">garden design</a> and garden design again.</p>', [{ anchor_text: "garden design", target_url: "/new" }]);
    expect(res.html).toBe('<p><a href="/old">garden design</a> and <a href="/new">garden design</a> again.</p>');
  });

  it("reports a link it couldn't place so the page can warn", () => {
    const res = insertLinks("<p>Hello <b>garden</b> design.</p>", [{ anchor_text: "garden design", target_url: "/g" }]);
    expect(res.placed).toBe(0);
  });
});
