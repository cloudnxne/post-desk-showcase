import { describe, it, expect } from "vitest";
import { postPlain, locateAnchor, checkLinks, normUrl } from "../src/core.js";
import { POST_MD, KNOWN } from "./fixtures.js";

const { plain, headings } = postPlain(POST_MD);
const known = new Set(KNOWN.map(normUrl));
const fresh = () => ({ urls: new Set(), anchors: new Set() });

describe("postPlain", () => {
  it("strips Markdown heading marks and records heading lines", () => {
    expect(plain).not.toMatch(/^#/m);
    expect(headings.has("choosing a patio")).toBe(true);
    expect(headings.has("sealing your patio")).toBe(true);
  });

  it("reads HTML posts too", () => {
    const r = postPlain("<h2>Our Services</h2><p>We fit <b>composite decking</b> fast.</p>");
    expect(r.plain).toBe("Our Services\nWe fit composite decking fast.");
    expect(r.headings.has("our services")).toBe(true);
  });
});

describe("locateAnchor: anchors must exist in the post", () => {
  it("finds a phrase and returns the post's own casing and sentence", () => {
    const hit = locateAnchor(plain, "GARDEN DESIGN SERVICE", headings);
    expect(hit.anchor).toBe("garden design service");
    expect(hit.sentence).toBe("Our garden design service maps out every bed, path and seating area before a spade goes in.");
  });

  it("rejects a phrase that isn't in the post", () => {
    expect(locateAnchor(plain, "decking installation", headings)).toBeNull();
  });

  it("rejects reworded or pluralised anchors", () => {
    expect(locateAnchor(plain, "patio installations", headings)).toBeNull();
  });

  it("matches whole words only", () => {
    expect(locateAnchor("We love a party.", "art")).toBeNull();
  });

  it("never uses a phrase that only appears in a heading", () => {
    expect(locateAnchor(plain, "Choosing a Patio", headings)).toBeNull();
  });

  it("skips a heading match and uses the body copy further down", () => {
    const p = postPlain("## Patio installation\n\nWe offer patio installation across Watford.");
    const hit = locateAnchor(p.plain, "patio installation", p.headings);
    expect(hit.sentence).toBe("We offer patio installation across Watford.");
  });

  it("ignores curly quotes, long dashes and wrapping punctuation", () => {
    const text = "It’s a well‑known fact – patios last. Ask about our client’s garden.";
    expect(locateAnchor(text, "our client's garden.")).toEqual({ anchor: "our client’s garden", sentence: "Ask about our client’s garden." });
    expect(locateAnchor(text, "“patios last”")?.anchor).toBe("patios last");
  });

  it("refuses anchors under three characters", () => {
    expect(locateAnchor(plain, "a ")).toBeNull();
  });
});

describe("checkLinks: every link is checked against the sitemap and the post", () => {
  const suggestions = [
    { anchor_text: "garden design service", target_url: "https://example-landscapes.co.uk/services/garden-design" },
    { anchor_text: "patio installation", target_url: "https://EXAMPLE-landscapes.co.uk/services/patio-installation/?utm=x" },
    { anchor_text: "fake grass lawns", target_url: "https://example-landscapes.co.uk/services/artificial-grass/" },
    { anchor_text: "free survey", target_url: "https://competitor.com/survey/" },
    { anchor_text: "Watford", target_url: "https://example-landscapes.co.uk/services/garden-design/" },
    { anchor_text: "", target_url: "https://example-landscapes.co.uk/areas/watford/" },
    null,
  ];
  const taken = fresh();
  const { ok, bad } = checkLinks(suggestions, plain, known, taken, headings);

  it("keeps links whose URL is in the sitemap and anchor is in the post", () => {
    expect(ok.map(l => l.anchor_text)).toEqual(["garden design service", "patio installation"]);
    expect(ok.every(l => l.verified && l.on)).toBe(true);
    expect(ok[1].sentence).toMatch(/^We handle patio installation/);
  });

  it("sends anchors that aren't in the post back for a retry", () => {
    expect(bad.map(l => l.anchor_text)).toEqual(["fake grass lawns"]);
  });

  it("drops URLs outside the client's sitemap without retrying", () => {
    expect([...ok, ...bad].some(l => l.target_url.includes("competitor.com"))).toBe(false);
  });

  it("uses each target URL once", () => {
    expect(ok.filter(l => l.target_url.includes("garden-design"))).toHaveLength(1);
  });

  it("remembers what was used so the retry can't reuse an anchor", () => {
    const retry = checkLinks([{ anchor_text: "Garden Design Service", target_url: KNOWN[2] }], plain, known, taken, headings);
    expect(retry.ok).toHaveLength(0);
    expect(retry.bad).toHaveLength(1);
  });

  it("accepts a corrected anchor on retry", () => {
    const retry = checkLinks([{ anchor_text: "Artificial grass", target_url: KNOWN[2] }], plain, known, taken, headings);
    expect(retry.ok[0].anchor_text).toBe("Artificial grass");
  });
});
