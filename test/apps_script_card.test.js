// The side-panel add-on (apps-script/Card.gs) run against fake Google services: it builds its cards, runs a post
// through the (fake) API, and adds only the ticked links. Catches wiring mistakes before anyone installs it.
import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

// A CardService stand-in: every builder method returns the builder and records what was set.
function cardService() {
  const built = [];
  built.all = [];          // every builder, including widgets that are never built on their own
  const builder = (kind) => {
    const rec = { kind, calls: [] };
    built.all.push(rec);
    const proxy = new Proxy(rec, {
      get(t, name) {
        if (name === "rec") return rec;
        if (name === "build") return () => { built.push(rec); return rec; };
        if (name === "throwException") return () => { throw Object.assign(new Error("AUTH"), { rec }); };
        return (...args) => { rec.calls.push([name, ...args]); return proxy; };
      },
    });
    return proxy;
  };
  const enums = { SelectionInputType: { DROPDOWN: "D", RADIO_BUTTON: "R", CHECK_BOX: "C" }, TextButtonStyle: { FILLED: "F" },
                  OpenAs: { FULL_SIZE: "FULL" } };
  return { built, svc: new Proxy(enums, { get: (t, n) => (n in t ? t[n] : () => builder(n.replace(/^new/, ""))) }) };
}

const RESULT = {
  title_tag: "Caring for Artificial Grass | ALC", title_status: "good", primary_keyword: "",
  meta_descriptions: [{ text: "One", angle: "benefit-led", length: 3, status: "short" }, { text: "Two", angle: "question-led", length: 3, status: "short" }],
  internal_links: [{ anchor_text: "a good installation", sentence: "", target_url: "https://x/install/", reason: "", priority: "high" },
                   { anchor_text: "free quote", sentence: "", target_url: "https://x/quote/", reason: "", priority: "high" }],
  link_back_from: [{ source_url: "https://x/blog/", anchor_text: "grass care", reason: "" }],
  headings: [], lists: [],
  stats: { verified: 2, retrieval: "semantic", search_console: { property: "x" }, fell_back: null },
};

function setup({ blocked = false, authNeeded = false } = {}) {
  const { built, svc } = cardService();
  const store = { POST_DESK_API_URL: "https://post-desk.example.run.app/" }, cache = {}, requests = [];
  const props = { getProperty: (k) => store[k] ?? null, setProperty: (k, v) => { store[k] = v; } };
  const inserted = [];
  const doc = { getId: () => "doc1", getName: () => "My post", getBody: () => ({}) };
  const fakes = {
    CardService: svc,
    DocumentApp: {
      ElementType: { PARAGRAPH: "P", LIST_ITEM: "L", TABLE: "TB", TEXT: "TX" },
      ParagraphHeading: { NORMAL: "N", TITLE: "T", SUBTITLE: "S", HEADING1: "H1", HEADING2: "H2", HEADING3: "H3", HEADING4: "H4", HEADING5: "H5", HEADING6: "H6" },
      GlyphType: { BULLET: "B", NUMBER: "NUM" },
      getActiveDocument: () => { throw new Error("the side panel must not use the active document"); },
      openById: () => { throw new Error("the side panel must not use openById"); },
    },
    PropertiesService: { getUserProperties: () => props, getScriptProperties: () => props },
    CacheService: { getUserCache: () => ({ get: (k) => cache[k] ?? null, put: (k, v) => { cache[k] = v; } }) },
    ScriptApp: { getIdentityToken: () => "a.b.c", getOAuthToken: () => "ya29.me",
      AuthMode: { FULL: "FULL" }, AuthorizationStatus: { REQUIRED: "REQUIRED", NOT_REQUIRED: "NOT_REQUIRED" },
      getAuthorizationInfo: () => ({ getAuthorizationStatus: () => (authNeeded ? "REQUIRED" : "NOT_REQUIRED"),
                                     getAuthorizationUrl: () => "https://accounts.google.com/auth" }) },
    UrlFetchApp: {
      fetch(url, opts) {
        requests.push([url, opts]);
        if (url.startsWith("https://docs.googleapis.com/")) {
          if (url.endsWith(":batchUpdate")) return { getResponseCode: () => 200, getContentText: () => "{}" };
          if (blocked) return { getResponseCode: () => 403, getContentText: () => JSON.stringify({ error: { message: "The caller does not have permission" } }) };
          return { getResponseCode: () => 200, getContentText: () => JSON.stringify({ documentId: "doc1", title: "My post", body: { content: [] } }) };
        }
        const body = url.endsWith("/clients") ? { clients: [{ domain: "lawnco.example.co.uk", indexed: true, search_console: true },
                                                             { domain: "pavingco.example.co.uk", indexed: false, search_console: true }] }
          : url.endsWith("/generate") ? RESULT : url.endsWith("/copy") ? { url: "https://api/copy/abc" } : {};
        return { getResponseCode: () => 200, getContentText: () => JSON.stringify(body) };
      },
    },
    Utilities: {},
  };
  const code = readFileSync(resolve(__dirname, "../apps-script/Code.gs"), "utf8");
  const card = readFileSync(resolve(__dirname, "../apps-script/Card.gs"), "utf8");
  fakes.__inserted = inserted;
  const names = Object.keys(fakes);
  const api = new Function(...names, `${code}\n${card}\n
    postText_ = function () { return Array(60).fill("word").join(" "); };
    insertLinks = function (links) { __inserted.push(...links); return { added: links.map(l => l.anchor_text), missing: [] }; };
    getHtml = function () { return "<h2>Steps</h2>"; };
    return { onDocsHomepage, onRun, onInsert, onClientChange, onHtml, onAddClient };`)(...names.map((n) => fakes[n]));
  return { api, built, store, requests, inserted };
}

const ev = (inputs = {}) => ({ docs: { id: "doc1" }, commonEventObject: { formInputs:
  Object.fromEntries(Object.entries(inputs).map(([k, v]) => [k, { stringInputs: { value: [].concat(v) } }])) } });
const calls = (rec, name) => rec.calls.filter((c) => c[0] === name);

describe("side-panel add-on", () => {
  it("asks for access to the doc first, then shows every client", () => {
    const { api, built } = setup();
    api.onDocsHomepage({});                                      // no doc access yet
    expect(built.at(-1).calls.length).toBeGreaterThan(0);
    api.onDocsHomepage(ev());
    const items = built.all.flatMap((b) => calls(b, "addItem")).map((c) => c[1]);
    expect(items).toContain("lawnco.example.co.uk");
    expect(items).toContain("pavingco.example.co.uk (not read yet)");
  });

  it("runs a post through the API and shows the results", () => {
    const { api, built, requests } = setup();
    api.onRun(ev({ domain: "lawnco.example.co.uk", keyword: "artificial grass" }));
    const sent = JSON.parse(requests.find(([u]) => u.endsWith("/generate"))[1].payload);
    expect(sent.client.domain).toBe("lawnco.example.co.uk");
    expect(sent.sitemap_urls).toEqual([]);
    expect(sent.options).toEqual({ max_seconds: 45 });
    const boxes = built.all.flatMap((b) => calls(b, "addItem")).map((c) => c[1]);
    expect(boxes).toContain('"a good installation" → /install/');
  });

  it("adds only the ticked links", () => {
    const { api, inserted } = setup();
    api.onRun(ev({ domain: "lawnco.example.co.uk" }));
    api.onInsert(ev({ links: ["1"] }));
    expect(inserted.map((l) => l.anchor_text)).toEqual(["free quote"]);
  });

  it("keeps the shared client notes when the panel doesn't send them", () => {
    const { api, store } = setup();
    store["client:lawnco.example.co.uk"] = JSON.stringify({ name: "ALC", notes: "Friendly, Kent", priority: "" });
    api.onRun(ev({ domain: "lawnco.example.co.uk" }));
    expect(JSON.parse(store["client:lawnco.example.co.uk"]).notes).toBe("Friendly, Kent");
  });

  it("opens the HTML on its own page to copy", () => {
    const { api, built, requests } = setup();
    api.onHtml(ev());
    const sent = JSON.parse(requests.find(([u]) => u.endsWith("/copy"))[1].payload);
    expect(sent).toEqual({ html: "<h2>Steps</h2>", title: "My post" });
    const link = built.all.find((b) => b.kind === "OpenLink");
    expect(calls(link, "setUrl")[0][1]).toBe("https://api/copy/abc");
  });

  it("asks for access again, instead of crashing, when Google won't open the doc", () => {
    const { api, built, requests } = setup({ blocked: true });
    api.onDocsHomepage(ev());
    const text = built.all.flatMap((b) => calls(b, "setText")).map((c) => c[1]).join(" ");
    expect(text).toContain("can't open this doc yet");
    expect(text).toContain("Allow for this doc");
    api.onRun(ev({ domain: "lawnco.example.co.uk" }));
    const note = built.all.filter((b) => b.kind === "Notification").flatMap((b) => calls(b, "setText")).map((c) => c[1]);
    expect(note.at(-1)).toContain("can't open this doc");
    expect(requests.some(([u]) => u.endsWith("/generate"))).toBe(false);
  });

  it("sends your own sign-in so Post Desk can read your Search Console", () => {
    const { api, requests } = setup();
    api.onDocsHomepage(ev());
    const headers = requests.find(([u]) => u.endsWith("/clients"))[1].headers;
    expect(headers["X-Google-Access-Token"]).toBe("ya29.me");
    expect(headers.Authorization).toBe("Bearer a.b.c");
  });

  it("adds a client that isn't listed, for everyone", () => {
    const { api, built, store } = setup();
    api.onAddClient(ev({ newDomain: "https://www.SamsClient.co.uk/about/" }));
    expect(JSON.parse(store["clients:added"])).toEqual(["samsclient.co.uk"]);
    const items = built.all.flatMap((b) => calls(b, "addItem")).map((c) => c[1]);
    expect(items).toContain("samsclient.co.uk (not read yet)");
    api.onAddClient(ev({ newDomain: "not a website" }));
    const note = built.all.filter((b) => b.kind === "Notification").flatMap((b) => calls(b, "setText")).map((c) => c[1]);
    expect(note.at(-1)).toContain("doesn't look like a website");
  });

  it("shows Google's authorise screen when a new permission needs approving, not the doc loop", () => {
    const { api } = setup({ authNeeded: true });
    let err;
    try { api.onDocsHomepage(ev()); } catch (e) { err = e; }
    expect(err && err.message).toBe("AUTH");
    expect(calls(err.rec, "setAuthorizationUrl")[0][1]).toBe("https://accounts.google.com/auth");
  });

  it("says what Google said when it still can't open the doc", () => {
    const { api, built } = setup({ blocked: true });
    api.onDocsHomepage(ev());
    const text = built.all.flatMap((b) => calls(b, "setText")).map((c) => c[1]).join(" ");
    expect(text).toContain("Google said: The caller does not have permission (error 403)");
  });

  it("keeps an error on the panel instead of a pop-up that vanishes", () => {
    const { api, built } = setup();
    const r = api.onRun(ev({ domain: "" }));                    // no client picked
    const text = built.all.flatMap((b) => calls(b, "setText")).map((c) => c[1]).join(" ");
    expect(text).toContain("Couldn't finish");
    expect(text).toContain("Pick a client first.");
    expect(r.calls.some((c) => c[0] === "setNavigation")).toBe(true);
  });

  it("asks for the API address to be set when it's missing", () => {
    const { api, built, store } = setup();
    delete store.POST_DESK_API_URL;
    api.onDocsHomepage(ev());
    const text = built.all.flatMap((b) => calls(b, "setText")).map((c) => c[1]).join(" ");
    expect(text).toContain("POST_DESK_API_URL");
  });
});
