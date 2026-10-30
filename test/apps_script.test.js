// The Google Doc script (apps-script/Code.gs) run against a small fake Google Doc: what it sends as the post,
// and how it adds links (exact text, the right sentence, never a heading).
import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

const G = { BULLET: "B", NUMBER: "NUM", LATIN_LOWER: "LL", LATIN_UPPER: "LU", ROMAN_LOWER: "RL", ROMAN_UPPER: "RU" };
const H = { NORMAL: "N", TITLE: "T", SUBTITLE: "S", HEADING1: "H1", HEADING2: "H2", HEADING3: "H3", HEADING4: "H4", HEADING5: "H5", HEADING6: "H6" };
const T = { PARAGRAPH: "P", LIST_ITEM: "L", TABLE: "TB", TEXT: "TX" };

function makeDoc(paras) {
  const kids = paras.map(([type, heading, text]) => {
    const links = {};
    const p = { getType: () => type, getHeading: () => heading, getText: () => text, linked: [] };
    const t = {
      getText: () => text, getParent: () => p, asText: () => t,
      getLinkUrl: (i) => links[i] || null,
      setLinkUrl: (s, e, u) => { links[s] = u; p.linked.push([text.slice(s, e + 1), u]); },
    };
    Object.assign(p, { asText: () => t, asParagraph: () => p, t });
    return p;
  });
  const body = {
    getNumChildren: () => kids.length,
    getChild: (i) => kids[i],
    findText(pattern, from) {          // case-insensitive, like the harder real-world case
      const re = new RegExp(pattern, "gi");
      const startK = from ? kids.indexOf(from.p) : 0;
      for (let k = startK; k < kids.length; k++) {
        re.lastIndex = from && k === startK ? from.getEndOffsetInclusive() + 1 : 0;
        const m = re.exec(kids[k].getText());
        if (m) return { p: kids[k], getElement: () => kids[k].t, getStartOffset: () => m.index, getEndOffsetInclusive: () => m.index + m[0].length - 1 };
      }
      return null;
    },
  };
  return { body, kids };
}

function load(doc, { name = "Post", last = null } = {}) {
  const DocumentApp = { ParagraphHeading: H, ElementType: T, GlyphType: G,
    getActiveDocument: () => ({ getBody: () => doc.body, getName: () => name, getId: () => "doc1" }) };
  const CacheService = { getUserCache: () => ({ get: () => (last ? JSON.stringify(last) : null), put: () => {} }) };
  const code = readFileSync(resolve(__dirname, "../apps-script/Code.gs"), "utf8");
  return new Function("DocumentApp", "CacheService", `${code}; return { postText_, insertLinks, getHtml };`)(DocumentApp, CacheService);
}

const DOC = () => makeDoc([
  [T.PARAGRAPH, H.NORMAL, "SEO title: Old title"],
  [T.PARAGRAPH, H.TITLE, "Caring for Artificial Grass"],
  [T.PARAGRAPH, H.HEADING2, "A good installation matters"],
  [T.PARAGRAPH, H.NORMAL, "Artificial grass is easy. A Good Installation helps."],
  [T.PARAGRAPH, H.NORMAL, "Truly, a good installation makes the biggest difference (really)."],
  [T.LIST_ITEM, H.NORMAL, "Brush it monthly"],
]);

describe("Google Doc script", () => {
  it("sends the doc as Markdown, without the notes it added itself", () => {
    const md = load(DOC()).postText_();
    expect(md).toContain("# Caring for Artificial Grass");
    expect(md).toContain("## A good installation matters");
    expect(md).toContain("- Brush it monthly");
    expect(md).not.toContain("SEO title");
  });

  it("links the exact text in the right sentence, never the heading", () => {
    const doc = DOC();
    const out = load(doc).insertLinks([{ anchor_text: "a good installation", target_url: "https://x/install/",
      sentence: "Truly, a good installation makes the biggest difference (really)." }]);
    expect(out.added).toEqual(["a good installation"]);
    expect(doc.kids[4].linked).toEqual([["a good installation", "https://x/install/"]]);
    expect(doc.kids[2].linked).toEqual([]);
    expect(doc.kids[3].linked).toEqual([]);     // "A Good Installation" is a different case
  });

  it("escapes regex characters and reports what it couldn't find", () => {
    const out = load(DOC()).insertLinks([
      { anchor_text: "difference (really)", sentence: "", target_url: "https://x/r/" },
      { anchor_text: "not in the doc", sentence: "", target_url: "https://x/n/" },
      { anchor_text: "A good installation matters", sentence: "", target_url: "https://x/h/" },
    ]);
    expect(out.added).toEqual(["difference (really)"]);
    expect(out.missing).toEqual(["not in the doc", "A good installation matters"]);
  });
});

// A doc with formatting: each block is [type, heading, runs, list] and each run is [text, {bold, italic, link}].
function richDoc(blocks) {
  const kids = blocks.map(([type, heading, runs, list]) => {
    const text = runs.map((r) => r[0]).join("");
    const at = (i) => { let n = 0; for (const r of runs) { if (i < n + r[0].length) return r[1] || {}; n += r[0].length; } return {}; };
    const starts = []; let n = 0; for (const r of runs) { starts.push(n); n += r[0].length; }
    const t = { getType: () => T.TEXT, asText: () => t, getText: () => text, getTextAttributeIndices: () => starts,
                isBold: (i) => !!at(i).bold, isItalic: (i) => !!at(i).italic, getLinkUrl: (i) => at(i).link || null };
    const el = { getType: () => type, getHeading: () => heading, asText: () => t, asParagraph: () => el, asListItem: () => el,
                 getNumChildren: () => 1, getChild: () => t, getListId: () => list && list.id, getGlyphType: () => list && list.glyph };
    return el;
  });
  return { body: { getNumChildren: () => kids.length, getChild: (i) => kids[i] }, kids };
}

const P = (runs, heading = H.NORMAL) => [T.PARAGRAPH, heading, typeof runs === "string" ? [[runs]] : runs];
const B = (text) => P([[text, { bold: true }]]);

describe("Copy as HTML", () => {
  it("handles a typical writer's doc: brief above, bold title and headings, typed bullets, links", () => {
    const doc = richDoc([
      P("Keyword: artificial grass maintenance"),
      B("Client brief"),
      P("Word count: 800"),
      B("Caring for Artificial Grass"),                          // the title, same as the doc's name
      P([["Artificial grass needs a little care. A "], ["good installation", { link: "https://x/install/" }], [" helps."]]),
      B("How often should you brush it?"),
      P("• Brush weekly in summer"),
      P("• Monthly in winter: less use, less wear"),
      P([["Get a "], ["free quote", { link: "https://x/quote/", bold: true }], ["."]]),
    ]);
    expect(load(doc, { name: "Caring for Artificial Grass" }).getHtml()).toBe([
      '<p>Artificial grass needs a little care. A <a href="https://x/install/">good installation</a> helps.</p>',
      "<h2>How often should you brush it?</h2>",
      "<ul>\n  <li>Brush weekly in summer</li>\n  <li><strong>Monthly in winter:</strong> less use, less wear</li>\n</ul>",
      '<p>Get a <a href="https://x/quote/"><strong>free quote</strong></a>.</p>',
    ].join("\n\n"));
  });

  it("uses the headings and list items Claude spotted in the last run", () => {
    const doc = richDoc([
      P("Notes for the writer"),
      P("Keeping Your Lawn Looking New"),                        // title: Claude marked it level 1
      P("Why care matters"),
      P("It lasts longer."),
      P("Pets"),
      P("Rinse weekly"),
      P("Use a pet safe cleaner"),
    ]);
    const last = { headings: [{ text: "Keeping Your Lawn Looking New", level: 1 }, { text: "Why care matters", level: 2 }, { text: "Pets", level: 3 }],
                   lists: [{ type: "ul", items: ["Rinse weekly", "Use a pet safe cleaner"] }] };
    expect(load(doc, { name: "Draft 3", last }).getHtml()).toBe([
      "<h2>Why care matters</h2>", "<p>It lasts longer.</p>", "<h3>Pets</h3>",
      "<ul>\n  <li>Rinse weekly</li>\n  <li>Use a pet safe cleaner</li>\n</ul>",
    ].join("\n\n"));
  });

  it("follows the doc's own heading styles and lists, and never skips a level", () => {
    const doc = richDoc([
      P("Caring for Artificial Grass", H.TITLE),
      P("Artificial grass needs very little looking after, but a few simple jobs through the year will keep it " +
        "looking new for far longer, and they only take a few minutes each time you do them."),
      P("Steps", H.HEADING1),
      [T.LIST_ITEM, H.NORMAL, [["Dig out"]], { id: "a", glyph: G.NUMBER }],
      [T.LIST_ITEM, H.NORMAL, [["Lay the "], ["sub-base", { link: "https://x/sub/", bold: true }]], { id: "a", glyph: G.NUMBER }],
      P("Deep detail", H.HEADING4),
      [T.LIST_ITEM, H.NORMAL, [["Pets"]], { id: "b", glyph: G.BULLET }],
    ]);
    expect(load(doc).getHtml()).toBe([
      "<p>Artificial grass needs very little looking after, but a few simple jobs through the year will keep it looking new " +
        "for far longer, and they only take a few minutes each time you do them.</p>",
      "<h2>Steps</h2>",
      '<ol>\n  <li>Dig out</li>\n  <li>Lay the <a href="https://x/sub/"><strong>sub-base</strong></a></li>\n</ol>',
      "<h3>Deep detail</h3>",
      "<ul>\n  <li>Pets</li>\n</ul>",
    ].join("\n\n"));
  });

  it("never drops content when it can't tell which line is the title", () => {
    const doc = richDoc([
      B("A bold title that isn't the doc name"),
      P("Intro paragraph."),
      P("Section", H.HEADING1),
      P("Body."),
    ]);
    const html = load(doc, { name: "Untitled document" }).getHtml();
    expect(html).toContain("Intro paragraph.");
    expect(html).toContain("<h2>Section</h2>");
  });

  it("handles a real patio sub-base post: brief line, title, bold headings and a Q and A section", () => {
    const doc = richDoc([
      P("IM Consultant: Sam Consultant Copywriter: Alex Writer"),
      B("Patio Sub-Base: Depths, Materials and Reinforcing Mesh Explained"),
      P("What lies beneath the patio is just as important as the slabs themselves."),
      B("What is a sub-base and why is it important? "),
      P([["Put simply, a "], ["sub-base for paving", { link: "https://rj/sub-base/" }], [" is a compacted layer of aggregate."]]),
      B("How deep should a patio base be?"),
      P("For a standard domestic garden patio, a patio base depth of approximately 100mm is commonly used."),
      B("Frequently asked questions about patio sub-bases"),
      P("Q. Can you lay a patio without a sub-base?"),
      P("A. While it is possible to lay patio slabs straight onto soil, it's not considered a good idea."),
      B("Q. What is MOT Type 1 and is it the best hardcore for a patio?"),
      P("A. MOT Type 1 is a mixture of crushed stones."),
      P([["Are you planning a new patio? "], ["Get in touch", { link: "https://rj/contact/" }], [" today."]]),
    ]);
    expect(load(doc, { name: "Paving & Co - patio sub-base blog" }).getHtml()).toBe([
      "<p>What lies beneath the patio is just as important as the slabs themselves.</p>",
      "<h2>What is a sub-base and why is it important?</h2>",
      '<p>Put simply, a <a href="https://rj/sub-base/">sub-base for paving</a> is a compacted layer of aggregate.</p>',
      "<h2>How deep should a patio base be?</h2>",
      "<p>For a standard domestic garden patio, a patio base depth of approximately 100mm is commonly used.</p>",
      "<h2>Frequently asked questions about patio sub-bases</h2>",
      "<h3>Can you lay a patio without a sub-base?</h3>",
      "<p>While it is possible to lay patio slabs straight onto soil, it's not considered a good idea.</p>",
      "<h3>What is MOT Type 1 and is it the best hardcore for a patio?</h3>",
      "<p>MOT Type 1 is a mixture of crushed stones.</p>",
      '<p>Are you planning a new patio? <a href="https://rj/contact/">Get in touch</a> today.</p>',
    ].join("\n\n"));
  });

  it("finds a plain-text title (not bold, not styled, not the doc name) below the brief line", () => {
    const doc = richDoc([
      P("IM Consultant: Sam Consultant Copywriter: Alex Writer"),
      P("Patio Sub-Base: Depths, Materials and Reinforcing Mesh Explained"),
      P("What lies beneath the patio is just as important as the slabs themselves, as poor groundwork can lead to sinking " +
        "slabs, uneven surfaces, standing water and cracks, so our team explains it all here."),
      P("What is a sub-base and why is it important?", H.HEADING2),
      P("Put simply, it is a compacted layer of aggregate."),
    ]);
    const html = load(doc, { name: "Untitled document" }).getHtml();
    expect(html.startsWith("<p>What lies beneath the patio")).toBe(true);
    expect(html).not.toContain("IM Consultant");
    expect(html).not.toContain("Explained");
    expect(html).toContain("<h2>What is a sub-base and why is it important?</h2>");
  });

  it("handles an agency doc template exactly as a real doc is built", () => {
    // From the real doc: SEO notes, client name in Title style, consultant/copywriter in Subtitle style, the post title
    // in Heading 1, section headings as bold lines, a Q and A section, links.
    const doc = richDoc([
      P("SEO title: Patio Sub-Base Depth, Materials & Mesh Explained"),
      P("Meta description: Patio sub-base explained: how deep it should be."),
      P("Paving & Co (London)", H.TITLE),
      P("IM Consultant: Sam Consultant                        Copywriter: Alex Writer", H.SUBTITLE),
      P([["Patio Sub-Base: Depths, Materials and Reinforcing Mesh Explained", { bold: true }]], H.HEADING1),
      P([["What lies beneath the patio is just as important as the slabs themselves, as poor groundwork can lead to "],
         ["sinking slabs, uneven surfaces", { link: "https://www.pavingco.example.co.uk/blog/why-do-patio-slabs-become-loose-or-uneven/" }],
         [", standing water and cracks."]]),
      B("What is a sub-base and why is it important? "),
      P("Put simply, a sub-base for paving is a compacted layer of aggregate."),
      B("Frequently asked questions about patio sub-bases"),
      B("Q. Can you lay a patio without a sub-base?"),
      P("A. While it is possible to lay patio slabs straight onto soil or sand, it's not considered a good idea."),
      B("Q. Does reinforcing mesh stop a patio from cracking?"),
      P("A. No, reinforcing mesh can't completely stop cracks from appearing."),
      P([["Simply "], ["get in touch", { link: "https://www.pavingco.example.co.uk/contact/" }], [" with us today."]]),
    ]);
    expect(load(doc, { name: "Paving & Co (London) 202610 - Patio Sub Base: Depths, Materials and Reinforcing Mesh Explained" })
      .getHtml()).toBe([
      '<p>What lies beneath the patio is just as important as the slabs themselves, as poor groundwork can lead to ' +
        '<a href="https://www.pavingco.example.co.uk/blog/why-do-patio-slabs-become-loose-or-uneven/">sinking slabs, uneven surfaces</a>, standing water and cracks.</p>',
      "<h2>What is a sub-base and why is it important?</h2>",
      "<p>Put simply, a sub-base for paving is a compacted layer of aggregate.</p>",
      "<h2>Frequently asked questions about patio sub-bases</h2>",
      "<h3>Can you lay a patio without a sub-base?</h3>",
      "<p>While it is possible to lay patio slabs straight onto soil or sand, it's not considered a good idea.</p>",
      "<h3>Does reinforcing mesh stop a patio from cracking?</h3>",
      "<p>No, reinforcing mesh can't completely stop cracks from appearing.</p>",
      '<p>Simply <a href="https://www.pavingco.example.co.uk/contact/">get in touch</a> with us today.</p>',
    ].join("\n\n"));
  });

  it("leaves out the SEO notes Post Desk adds", () => {
    const doc = richDoc([P("SEO title: x"), P("Meta description: y"), P("Body text here.")]);
    expect(load(doc).getHtml()).toBe("<p>Body text here.</p>");
  });
});

// ---------- the side panel's Google Docs API reader ----------
function loadRest(json) {
  const requests = [];
  const DocumentApp = { ParagraphHeading: H, ElementType: T, GlyphType: G };
  const UrlFetchApp = { fetch(url, opts) {
    requests.push([url, opts]);
    if (url.endsWith(":batchUpdate")) return { getResponseCode: () => 200, getContentText: () => "{}" };
    return { getResponseCode: () => 200, getContentText: () => JSON.stringify(json) };
  } };
  const ScriptApp = { getOAuthToken: () => "ya29.me" };
  const CacheService = { getUserCache: () => ({ get: () => null, put: () => {} }) };
  const code = readFileSync(resolve(__dirname, "../apps-script/Code.gs"), "utf8");
  const api = new Function("DocumentApp", "UrlFetchApp", "ScriptApp", "CacheService",
    `${code}; return { useDoc_, getHtml, insertLinks, postText_, addSeoNotes };`)(DocumentApp, UrlFetchApp, ScriptApp, CacheService);
  api.useDoc_({ docs: { id: "doc9" } });
  return { api, requests };
}

// Builds Docs API paragraphs with real start indices. A run is [text, style] or { image: true } (one index, no text).
function restJson(paras, lists = {}) {
  let i = 1;
  const content = [{ startIndex: 0, endIndex: 1, sectionBreak: {} }];
  for (const [style, runs, bullet] of paras) {
    const start = i;
    const elements = [];
    const all = [...runs, ["\n"]];
    for (const r of all) {
      if (r.image) { elements.push({ startIndex: i, endIndex: i + 1, inlineObjectElement: {} }); i += 1; continue; }
      const [text, st = {}] = r;
      elements.push({ startIndex: i, endIndex: i + text.length, textRun: { content: text, textStyle: st } });
      i += text.length;
    }
    content.push({ startIndex: start, endIndex: i, paragraph: { elements, paragraphStyle: { namedStyleType: style }, ...(bullet ? { bullet } : {}) } });
  }
  return { documentId: "doc9", title: "Paving & Co (London) 202610 - Patio Sub Base", body: { content }, lists };
}

describe("side panel through the Google Docs API", () => {
  const json = () => restJson([
    ["NORMAL_TEXT", [["SEO title: old"]]],
    ["TITLE", [["Paving & Co (London)"]]],
    ["SUBTITLE", [["IM Consultant: Sam Consultant   Copywriter: Alex Writer"]]],
    ["HEADING_1", [["Patio Sub-Base: Depths, Materials and Reinforcing Mesh Explained", { bold: true }]]],
    ["NORMAL_TEXT", [["What lies beneath the patio matters, as poor groundwork can lead to "],
                     ["sinking slabs", { link: { url: "https://rj/loose/" } }], [" and cracks."]]],
    ["NORMAL_TEXT", [["How deep should a patio base be?", { bold: true }]]],
    ["NORMAL_TEXT", [{ image: true }, ["A patio base depth of approximately 100mm is commonly used by a landscaping professional."]]],
    ["NORMAL_TEXT", [["Steps", { bold: true }]]],
    ["NORMAL_TEXT", [["Dig out"]], { listId: "L1", nestingLevel: 0 }],
    ["NORMAL_TEXT", [["Compact the "], ["sub-base", { italic: true }]], { listId: "L1", nestingLevel: 0 }],
  ], { L1: { listProperties: { nestingLevels: [{ glyphType: "DECIMAL" }] } } });

  it("builds the same SEO HTML from the Docs API as from the doc itself", () => {
    const { api } = loadRest(json());
    expect(api.getHtml()).toBe([
      '<p>What lies beneath the patio matters, as poor groundwork can lead to <a href="https://rj/loose/">sinking slabs</a> and cracks.</p>',
      "<h2>How deep should a patio base be?</h2>",
      "<p>A patio base depth of approximately 100mm is commonly used by a landscaping professional.</p>",
      "<h2>Steps</h2>",
      "<ol>\n  <li>Dig out</li>\n  <li>Compact the <em>sub-base</em></li>\n</ol>",
    ].join("\n\n"));
  });

  it("adds links at the right place in the doc, even after an image", () => {
    const j = json();
    const { api, requests } = loadRest(j);
    const out = api.insertLinks([{ anchor_text: "landscaping professional", sentence: "", target_url: "https://rj/hard-landscaping/" }]);
    expect(out.added).toEqual(["landscaping professional"]);
    const sent = JSON.parse(requests.find(([u]) => u.endsWith(":batchUpdate"))[1].payload).requests;
    const para = j.body.content[7].paragraph;                    // the paragraph that starts with the image
    const textStart = para.elements[1].startIndex;               // its text begins one index after the image
    const at = para.elements[1].textRun.content.indexOf("landscaping professional");
    expect(sent).toEqual([{ updateTextStyle: { range: { startIndex: textStart + at, endIndex: textStart + at + "landscaping professional".length },
                                               textStyle: { link: { url: "https://rj/hard-landscaping/" } }, fields: "link" } }]);
  });

  it("never links an existing link or a heading", () => {
    const { api, requests } = loadRest(json());
    const out = api.insertLinks([{ anchor_text: "sinking slabs", sentence: "", target_url: "https://x/" },
                                 { anchor_text: "Patio Sub-Base", sentence: "", target_url: "https://x/" }]);
    expect(out.missing).toEqual(["sinking slabs", "Patio Sub-Base"]);
    expect(requests.some(([u]) => u.endsWith(":batchUpdate"))).toBe(false);
  });

  it("adds the title and meta at the top as plain notes", () => {
    const { api, requests } = loadRest(json());
    api.addSeoNotes("New title", "New meta");
    const sent = JSON.parse(requests.find(([u]) => u.endsWith(":batchUpdate"))[1].payload).requests;
    expect(sent[0]).toEqual({ insertText: { location: { index: 1 }, text: "SEO title: New title\nMeta description: New meta\n" } });
    expect(sent[1].updateParagraphStyle.paragraphStyle.namedStyleType).toBe("NORMAL_TEXT");
  });
});
