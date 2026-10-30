// Writes service/tests/golden.json: inputs and the outputs src/core.js gives for them.
// The Python service must give the same answers (service/tests/test_parity.py), so the page and the API
// never disagree about which anchors and links are valid. CI regenerates this file and fails if it changed.
import { writeFile } from "node:fs/promises";
import { JSDOM } from "jsdom";

const { window } = new JSDOM("");
globalThis.document = window.document;
const core = await import("../src/core.js");
const { POST_MD, KNOWN, SITEMAP_XML } = await import("../test/fixtures.js");

const POST_HTML = `<h1>Composite Decking Guide</h1><p>Our <b>composite decking</b> range won&rsquo;t rot. It&#39;s low maintenance.</p>
<h2>Composite decking colours</h2><ul><li>Grey: modern look</li><li>Oak: warm &amp; classic</li></ul>
<div><p>Fitted across Essex. Ask about decking installation today!</p></div><script>var x = "hidden text";</script>`;

const POSTS = {
  md: POST_MD,
  html: POST_HTML,
  plain: "Why choose us?\nWe’re a family firm – est. 1998. Our team lays patios. Call 01234 567890 for a quote!\n\nPatios, paths and driveways.",
  tricky: "U.S. firms say “quality first”. Is it worth it? Yes! 3.5 stars isn't bad. e.g. the party was art.\n\n## art\n\nThe art of patio laying.",
};

const ANCHORS = {
  md: ["garden design service", "GARDEN DESIGN SERVICE", "patio installation", "patio installations", "Choosing a Patio", "Sealing your patio",
    "Artificial grass", "free survey", "Watford.", "“free survey”", "a ", "", "gravel", "Survey the site", "decking"],
  html: ["composite decking", "Composite Decking", "won't rot", "won’t rot", "low maintenance", "decking installation", "Essex", "hidden text", "warm & classic", "Oak"],
  plain: ["family firm", "family firm - est", "family firm – est", "lays patios", "quote", "Patios, paths", "driveways", "Why choose us"],
  tricky: ["quality first", "\"quality first\"", "worth it", "3.5 stars", "art", "patio laying", "party", "U.S. firms"],
};

const SLUG_URLS = ["https://x.com/", "https://x.com", "https://x.com/services/patio%20laying/", "https://x.com/a/b/c?q=1#h", "not a url", "https://x.com/caf%C3%A9/"];
const NORM_URLS = [" https://X.com/Page/?utm=1#top ", "https://x.com/a#b?c", "", "https://x.com/"];

const RANK_URLS = [...Array.from({ length: 30 }, (_, i) => `https://x.com/misc/page-${i}/`), ...KNOWN,
  "https://example-landscapes.co.uk/blog/designing-small-gardens/", "https://example-landscapes.co.uk/services/landscaping/gardens/design/"];

const out = { postPlain: {}, locateAnchor: [], checkLinks: {}, slugOf: [], normUrl: [], tokens: [], parseSitemap: {}, rankUrls: [], metaStatus: [] };

for (const [name, content] of Object.entries(POSTS)) {
  const { plain, headings } = core.postPlain(content);
  out.postPlain[name] = { content, plain, headings: [...headings].sort() };
  for (const anchor of ANCHORS[name]) out.locateAnchor.push({ post: name, anchor, result: core.locateAnchor(plain, anchor, headings) });
}

{
  const { plain, headings } = core.postPlain(POST_MD);
  const known = new Set(KNOWN.map(core.normUrl));
  const raw = [
    { anchor_text: "garden design service", target_url: "https://example-landscapes.co.uk/services/garden-design", reason: "r", priority: "high" },
    { anchor_text: "patio installation", target_url: "https://EXAMPLE-landscapes.co.uk/services/patio-installation/?utm=x", reason: "r", priority: "high" },
    { anchor_text: "fake grass lawns", target_url: KNOWN[2], reason: "r", priority: "medium" },
    { anchor_text: "free survey", target_url: "https://competitor.com/survey/" },
    { anchor_text: "Watford", target_url: KNOWN[0] },
    { anchor_text: "Garden design service", target_url: KNOWN[3] },
    { anchor_text: "", target_url: KNOWN[3] },
    null,
  ];
  const taken = { urls: new Set(), anchors: new Set() };
  const { ok, bad } = core.checkLinks(raw, plain, known, taken, headings);
  out.checkLinks = { raw, known: KNOWN, ok: ok.map(({ on, ...l }) => l), bad, taken: { urls: [...taken.urls].sort(), anchors: [...taken.anchors].sort() } };
}

for (const u of SLUG_URLS) out.slugOf.push({ url: u, result: core.slugOf(u) });
for (const u of NORM_URLS) out.normUrl.push({ url: u, result: core.normUrl(u) });
for (const s of [POST_MD, "The Quick-brown fox's 42 jumps; café über", ""]) out.tokens.push({ text: s, result: core.tokens(s) });
out.parseSitemap = { xml: SITEMAP_XML, result: core.parseSitemap(SITEMAP_XML) };
for (const [content, keyword, limit] of [[POST_MD, "garden design", 5], [POST_MD, "", 3], ["patios patios patios", "patio", 2], ["anything", "", 100]])
  out.rankUrls.push({ urls: RANK_URLS, content, keyword, limit, result: core.rankUrls(RANK_URLS, content, keyword, limit) });
for (const n of [0, 119, 120, 160, 161]) out.metaStatus.push({ len: n, result: core.metaStatus(n) });

// The prompts: lift the two template literals out of the page and fill them, so the service sends the same words.
{
  const page = await (await import("node:fs/promises")).readFile(new URL("../src/page.html", import.meta.url), "utf8");
  // Each prompt template ends at the first backtick followed by ";" (nested templates inside end with other characters).
  const tpl = (startMark) => { const i = page.indexOf(startMark) + startMark.length - 1; return page.slice(i, page.indexOf("`;", i) + 1); };
  const genTpl = tpl("const prompt = `"), retryTpl = tpl("const retry = `");
  const c = { name: "Example Landscapes", domain: "example-landscapes.co.uk", notes: "Friendly, local, no jargon" };
  const priority = ["https://example-landscapes.co.uk/services/garden-design/"];
  const draft = { title: "Planning Your Dream Garden", keyword: "garden design", url: "" };
  const candidates = [...priority, ...KNOWN.slice(1)], all = [...KNOWN, "https://example-landscapes.co.uk/"];
  const content = POST_MD;
  const genPrompt = new Function("c", "priority", "draft", "candidates", "all", "content", "return " + genTpl)(c, priority, draft, candidates, all, content);
  const blank = new Function("c", "priority", "draft", "candidates", "all", "content", "return " + genTpl)(
    { name: "Bare Client" }, [], { title: "", keyword: "", url: "" }, KNOWN, KNOWN, "Short post.");
  const bad = [{ target_url: KNOWN[2], anchor_text: "fake grass lawns" }];
  const taken = { anchors: new Set(["garden design service", "patio installation"]) };
  const plain = core.postPlain(POST_MD).plain;
  const retryPrompt = new Function("taken", "bad", "plain", "return " + retryTpl)(taken, bad, plain);
  out.prompts = {
    generate: { client: { ...c, priority_pages: priority }, post: { ...draft, content }, candidates, total: all.length, priority, prompt: genPrompt },
    generateBlank: { client: { name: "Bare Client" }, post: { title: "", keyword: "", url: "", content: "Short post." }, candidates: KNOWN, total: KNOWN.length, priority: [], prompt: blank },
    retry: { bad, used: [...taken.anchors], plain, prompt: retryPrompt },
  };
}

await writeFile(new URL("../service/tests/golden.json", import.meta.url), JSON.stringify(out, null, 1) + "\n");
console.log(`Wrote service/tests/golden.json: ${out.locateAnchor.length} anchor cases, ${out.rankUrls.length} ranking cases`);
