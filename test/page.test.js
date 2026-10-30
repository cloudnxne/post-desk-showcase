// @vitest-environment node
// End-to-end check of the built page: a fake claude.ai runtime (database + Claude), one client, one post.
// Claude's first answer has one anchor that isn't in the post and one URL outside the sitemap,
// so this covers the retry loop and the wiring between the page and core.js.
import { describe, it, expect, beforeAll } from "vitest";
import { readFile } from "node:fs/promises";
import { JSDOM, VirtualConsole } from "jsdom";
import { inline } from "../scripts/inline.mjs";
import { POST_MD, KNOWN } from "./fixtures.js";

const wait = ms => new Promise(r => setTimeout(r, ms));

function fakeRuntime(){
  const store = new Map([
    ["clients/c1", { name: "Example Landscapes", domain: "example-landscapes.co.uk", notes: "", priority: "", chunks: 1, urlCount: KNOWN.length, createdAt: 1 }],
    ["clients/c1/sitemap/c0", { urls: KNOWN }],
  ]);
  const prompts = [];
  let added = 0;
  const children = prefix => [...store].filter(([k]) => k.startsWith(prefix + "/") && !k.slice(prefix.length + 1).includes("/"));
  const collection = path => {
    const q = {
      orderBy: () => q, limit: () => q,
      onSnapshot: ok => { setTimeout(() => ok({ docs: children(path).map(([k, v]) => ({ id: k.split("/").pop(), data: () => v })) })); return () => {}; },
      add: async d => { added++; store.set(`${path}/p${added}`, d); return { id: "p" + added }; },
    };
    return q;
  };
  const doc = path => ({
    get: async () => ({ exists: store.has(path), data: () => store.get(path) }),
    set: async d => store.set(path, d), update: async d => store.set(path, { ...store.get(path), ...d }), delete: async () => store.delete(path),
  });
  const sample = { json: async prompt => {
    prompts.push(prompt);
    if (prompts.length > 1) return { internal_links: [{ anchor_text: "Artificial grass", target_url: KNOWN[2], reason: "r", priority: "medium" }] };
    return {
      title_tag: "Planning Your Dream Garden | Example", primary_keyword: "garden design",
      meta_descriptions: [{ text: "x".repeat(150), angle: "benefit" }, { text: "y".repeat(170), angle: "question" }],
      internal_links: [
        { anchor_text: "garden design service", target_url: KNOWN[0], reason: "r", priority: "high" },
        { anchor_text: "patio installation", target_url: KNOWN[1], reason: "r", priority: "high" },
        { anchor_text: "fake grass lawns", target_url: KNOWN[2], reason: "r", priority: "medium" },
        { anchor_text: "free survey", target_url: "https://competitor.com/", reason: "r", priority: "low" },
      ],
      link_back_from: [], headings: [], lists: [],
    };
  } };
  return { store, prompts, saved: () => added, claude: { use: async n => n === "db" ? { collection, doc } : n === "sample" ? sample : { can: async () => true } } };
}

describe("built page, end to end", () => {
  let d, rt, errors = [];

  beforeAll(async () => {
    const page = await readFile(new URL("../src/page.html", import.meta.url), "utf8");
    const core = await readFile(new URL("../src/core.js", import.meta.url), "utf8");
    rt = fakeRuntime();
    const vc = new VirtualConsole(); vc.on("jsdomError", e => errors.push(e.message));
    const dom = new JSDOM(`<!doctype html><html><body>${inline(page, core)}</body></html>`, {
      runScripts: "dangerously", url: "https://claude.ai/", virtualConsole: vc,
      beforeParse(w){ w.claude = rt.claude; },
    });
    const w = dom.window; d = w.document;
    await wait(200);
    const type = (sel, val) => { const el = d.querySelector(sel); el.value = val; el.dispatchEvent(new w.Event("input")); };
    type("#p-title", "Planning Your Dream Garden");
    type("#p-content", POST_MD);
    d.querySelector("#genBtn").click();
    for (let i = 0; i < 50 && !/^Done/.test(d.querySelector("#genStatus")?.textContent || ""); i++) await wait(50);
  });

  it("runs without errors", () => expect(errors).toEqual([]));

  it("asks Claude once, then once more to fix the bad anchor", () => {
    expect(rt.prompts).toHaveLength(2);
    expect(rt.prompts[1]).toContain('rejected anchor: "fake grass lawns"');
    expect(rt.prompts[1]).not.toContain("competitor.com");
  });

  it("shows only verified links, in post order", () => {
    expect([...d.querySelectorAll("#links .ctx mark")].map(m => m.textContent)).toEqual(["garden design service", "patio installation", "Artificial grass"]);
    expect(d.querySelector("#genStatus").textContent).toMatch(/^Done\. 3 links.*1 suggestion dropped/);
  });

  it("places every ticked link in the HTML with a clean outline", () => {
    expect(d.querySelector("#htmlStatus").textContent).toBe("2 H2 · 1 H3 · 2 lists · 3 links placed.");
    const preview = d.querySelector("#preview");
    expect(preview.querySelectorAll("a")).toHaveLength(3);
    expect(preview.querySelector("h1")).toBeNull();
  });

  it("flags meta length on each option", () => {
    expect([...d.querySelectorAll("#metas .meter")].map(m => m.textContent)).toEqual(["150 chars · good", "170 chars · may truncate"]);
  });

  it("saves the run to the client's history", () => expect(rt.saved()).toBe(1));
});
