// Post Desk core: the pure logic behind the page, with no UI, storage or Claude calls.
// The build script inlines this file into the page, so what the tests check is what ships.
// Functions that need a DOM take an optional `doc` (the browser's document by default, jsdom in tests).

export const STOP = new Set("the a an and or but of to in on for with at by from as is are was were be been it its this that these those you your we our they their he she his her i me my not no yes can will would should could how what why when where which who about into over under more most less than then so if do does did have has had just also very all any each some such only own same too out up down off again further once here there both few other new page html www com blog category tag".split(" "));

export const META_MIN = 120, META_MAX = 160, TITLE_MAX = 60;

export const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));

// Folds curly quotes and long dashes to plain ones and lower-cases, so matching ignores typography.
export const fold = s => String(s).replace(/[‘’ʼ]/g,"'").replace(/[“”]/g,'"').replace(/[–—]/g,"-").toLowerCase();

export const tokens = s => (String(s).toLowerCase().match(/[a-z0-9]+/g) || []).filter(w => w.length > 2 && !STOP.has(w));
export const slugOf = u => { try { const p = new URL(u).pathname; return p === "/" ? "(home page)" : decodeURIComponent(p); } catch { return u; } };
export const normUrl = u => (u || "").trim().replace(/[#?].*$/,"").replace(/\/$/,"").toLowerCase();

// ---------- sitemap ----------
export function parseSitemap(text){
  const locs = [...text.matchAll(/<loc>\s*(?:<!\[CDATA\[)?\s*([^<\]]+?)\s*(?:\]\]>)?\s*<\/loc>/gi)].map(m => m[1].trim());
  const raw = locs.length ? locs : text.split(/[\s,]+/).filter(s => /^https?:\/\//i.test(s));
  const isIndex = /<sitemapindex/i.test(text);
  const urls = [...new Set(raw.map(u => u.replace(/&amp;/g,"&")))].filter(u => !/\.(xml|jpg|jpeg|png|gif|webp|pdf)(\?|$)/i.test(u));
  return {urls, isIndex, childSitemaps: isIndex ? locs : []};
}

// Ranks sitemap URLs by how many of the post's words appear in the URL slug.
export function rankUrls(urls, content, keyword, limit){
  if (urls.length <= limit) return urls;
  const freq = new Map();
  for (const w of tokens(content + " " + keyword + " " + keyword)) freq.set(w, (freq.get(w)||0)+1);
  const scored = urls.map(u => {
    let s = 0;
    for (const t of tokens(slugOf(u))) {
      if (freq.has(t)) s += 1 + Math.log(freq.get(t));
      else if (t.length >= 5) { for (const [w,f] of freq) if (w.length>=5 && (w.startsWith(t.slice(0,5)) )) { s += .5 + Math.log(f)/2; break; } }
    }
    const depth = (slugOf(u).match(/\//g)||[]).length;
    return {u, s: s - depth*0.05};
  });
  scored.sort((a,b) => b.s - a.s);
  return scored.slice(0, limit).map(x => x.u);
}

// How well a post fits a client's site: top post words found in its page URLs.
export function siteFit(urls, domain, content){
  const freq = new Map();
  for (const w of tokens(content)) freq.set(w, (freq.get(w)||0)+1);
  const top = [...freq].sort((a,b) => b[1]-a[1]).slice(0, 30);
  const site = new Set(tokens(urls.map(slugOf).join(" ") + " " + (domain || "")));
  return top.reduce((s, [w, f]) => s + (site.has(w) ? 1 + Math.log(f) : 0), 0);
}

// ---------- length checks ----------
export const metaStatus = len => len < META_MIN ? "short" : len <= META_MAX ? "good" : "may truncate";
export const titleStatus = len => len <= TITLE_MAX ? "good" : "may truncate";

// ---------- lists and headings ----------
// Bullet markers people paste: - * • ● ▪ ◦ ‣ ► ▸ ➤ → ✓ ✔ ☐ ☑ ◆ ■ □, Google Docs' "o", and numbered "1." "1)" "a)".
export const MARKER_UL = /^(?:[-*+]\s+|[•●▪◦‣►▸➤→✓✔☐☑◆■□–—]\s*|o\s+(?=[A-Z0-9]))/;
export const MARKER_OL = /^(?:\d{1,2}[.)]|\(\d{1,2}\)|[a-h][.)])\s+/;
export const MARKER_HTML = /^\s*(?:[-*+]\s+|[•●▪◦‣►▸➤→✓✔☐☑◆■□–—]\s*|\d{1,2}[.)]\s+)/;
export function stripMarker(t){
  t = String(t).trim();
  if (MARKER_OL.test(t)) return {type: "ol", text: t.replace(MARKER_OL, "")};
  if (MARKER_UL.test(t)) return {type: "ul", text: t.replace(MARKER_UL, "")};
  return {type: null, text: t};
}
// "Label: explanation" items get the label in bold, a common scannable list style.
export const itemHTML = html => html.replace(/^([^:<>]{2,40}):\s+(?=\S)/, (m, label) => /^https?$/i.test(label) ? m : `<strong>${label}:</strong> `);

// Builds clean, SEO-structured HTML: at most one H1, sections as H2/H3, no skipped levels, lists as <ul>/<ol>.
export function buildSeoHTML(content, headings, title, lists, {includeH1 = false, doc = globalThis.document} = {}){
  const key = t => fold(String(t).replace(/^#{1,6}\s+/, "").replace(/[:\s]+$/, "").replace(/\s+/g, " ").trim());
  const hmap = new Map((headings || []).map(h => [key(h.text), Math.min(4, Math.max(1, +h.level || 2))]));
  // List items: bullet/number markers typed in the post, or lines Claude identified as list items (markers often vanish when pasting).
  const lmap = new Map();
  for (const l of lists || []) for (const it of l.items || []) lmap.set(key(stripMarker(it).text), l.type === "ol" ? "ol" : "ul");
  const listType = text => { const m = stripMarker(text); return m.type || lmap.get(key(m.text)) || null; };
  const blocks = [];
  let list = null;
  const pushItem = (type, html) => {
    if (!list || list.type !== type) { list = {type, items: []}; blocks.push({list}); }
    list.items.push(html);
  };
  if (/<(p|h[1-6]|ul|ol|div|br)\b/i.test(content)) {
    const root = doc.createElement("div"); root.innerHTML = content;
    root.querySelectorAll("script,style,iframe,object,embed,meta,link").forEach(n => n.remove());
    for (const el of root.querySelectorAll("*")) for (const at of [...el.attributes]) if (/^on|^style$|^class$|^id$/i.test(at.name)) el.removeAttribute(at.name);
    const walk = parent => { for (const el of [...parent.children]) {
      const tag = el.tagName.toLowerCase(), text = el.textContent.trim();
      if (!text && !el.querySelector("img")) continue;
      const lt = tag === "p" && !hmap.has(key(text)) ? listType(text) : null;
      if (lt) { pushItem(lt, itemHTML(el.innerHTML.trim().replace(MARKER_HTML, ""))); continue; }
      list = null;
      if (/^h[1-6]$/.test(tag)) blocks.push({h: +tag[1], html: el.innerHTML.trim(), text});
      else if (tag === "div" || tag === "section" || tag === "article") walk(el);
      else if (tag === "p" && hmap.has(key(text))) blocks.push({h: hmap.get(key(text)), html: el.innerHTML.trim(), text});
      else blocks.push({raw: el.outerHTML});
    } };
    walk(root);
  } else {
    for (const raw of content.split(/\r?\n/)) {
      const line = raw.trim();
      if (!line) continue;   // a blank line between bullets shouldn't split the list
      const md = line.match(/^(#{1,6})\s+(.*)$/);
      const lt = !md && !hmap.has(key(line)) ? listType(line) : null;
      if (lt) { pushItem(lt, itemHTML(esc(stripMarker(line).text))); continue; }
      list = null;
      if (md) blocks.push({h: md[1].length, html: esc(md[2]), text: md[2]});
      else if (hmap.has(key(line))) blocks.push({h: hmap.get(key(line)), html: esc(line), text: line});
      else blocks.push({html: esc(line)});
    }
  }
  // Title handling: the first heading (or a line matching the title) is the H1 candidate.
  const firstH = blocks.findIndex(b => b.h);
  let titleIdx = -1;
  if (firstH === 0 && (blocks[0].h === 1 || (title && key(blocks[0].text) === key(title)))) titleIdx = 0;
  else if (blocks.length && !blocks[0].h && title && key((blocks[0].html || "").replace(/<[^>]+>/g, "")) === key(title)) { blocks[0].h = 1; blocks[0].text = title; titleIdx = 0; }
  if (titleIdx === 0) { if (includeH1) blocks[0].h = 1; else blocks.shift(); }
  else if (includeH1 && title) blocks.unshift({h: 1, html: esc(title), text: title});
  let prev = 1, seenH1 = false;
  for (const b of blocks) {
    if (!b.h) continue;
    if (b.h === 1) { if (includeH1 && !seenH1) { seenH1 = true; prev = 1; continue; } b.h = 2; }
    b.h = Math.max(2, Math.min(b.h, prev + 1)); prev = b.h;
  }
  return blocks.map(b => b.raw ? b.raw : b.list ? `<${b.list.type}>\n${b.list.items.map(i => `  <li>${i}</li>`).join("\n")}\n</${b.list.type}>` : b.h ? `<h${b.h}>${b.html}</h${b.h}>` : `<p>${b.html}</p>`).join("\n\n");
}

// Puts each ticked link on the first whole-word match of its anchor in body text (never in a heading or an existing link).
export function insertLinks(html, links, {doc = globalThis.document} = {}){
  const root = doc.createElement("div"); root.innerHTML = html;
  let placed = 0;
  for (const l of links){
    const a = fold(l.anchor_text);
    const walker = doc.createTreeWalker(root, 4 /* NodeFilter.SHOW_TEXT */);
    let node, done = false;
    while (!done && (node = walker.nextNode())){
      if (node.parentElement.closest("a,h1,h2,h3,h4,h5,h6")) continue;
      const hay = fold(node.nodeValue);
      let i = -1, from = 0;
      while ((i = hay.indexOf(a, from)) >= 0){
        const b = hay[i-1], e = hay[i+a.length];
        if (!(b && /[a-z0-9]/.test(b)) && !(e && /[a-z0-9]/.test(e))) break;
        from = i + 1;
      }
      if (i < 0) continue;
      const mid = node.splitText(i); mid.splitText(a.length);
      const link = doc.createElement("a"); link.href = l.target_url;
      mid.replaceWith(link); link.append(mid);
      placed++; done = true;
    }
  }
  return {html: root.innerHTML, placed};
}

// ---------- anchor checks ----------
// Plain text of the post, one block per line, plus the set of heading lines (folded) so anchors can avoid them.
export function postPlain(content, {doc = globalThis.document} = {}){
  const isHTML = /<[a-z][\s\S]*>/i.test(content);
  const div = doc.createElement("div");
  div.innerHTML = isHTML ? content.replace(/<\/(p|h[1-6]|li|div)>/gi, "$&\n") : esc(content);
  const tidy = t => t.replace(/[ \t ]+/g, " ").trim();
  const headings = new Set(isHTML ? [...div.querySelectorAll("h1,h2,h3,h4,h5,h6")].map(h => fold(tidy(h.textContent)))
    : content.split("\n").filter(l => /^\s*#{1,6}\s/.test(l)).map(l => fold(tidy(l.replace(/^\s*#{1,6}\s+/, "")))));
  const plain = div.textContent.replace(/^#{1,6}\s+/gm, "").replace(/[ \t ]+/g, " ").replace(/\s*\n\s*/g, "\n").trim();
  return {plain, headings};
}

// Returns {anchor, sentence} using the post's own wording, or null when the phrase isn't in the body text.
export function locateAnchor(plain, anchor, headings = new Set()){
  const a = fold(String(anchor || "").replace(/\s+/g, " ").trim().replace(/^["'“‘]+|["'”’.,;:!?]+$/g, ""));
  if (a.length < 3) return null;
  const hay = fold(plain.replace(/\n/g, " "));
  let i = -1, from = 0;
  while ((i = hay.indexOf(a, from)) >= 0) {
    const before = hay[i-1], after = hay[i+a.length];
    const whole = !(before && /[a-z0-9]/.test(before)) && !(after && /[a-z0-9]/.test(after));
    const ls = plain.lastIndexOf("\n", i) + 1, le = plain.indexOf("\n", i);
    const inHeading = headings.has(fold(plain.slice(ls, le < 0 ? plain.length : le)));
    if (whole && !inHeading) break;   // whole words, body text only (never a heading)
    from = i + 1;
  }
  if (i < 0) return null;
  const flat = plain.replace(/\n/g, " ");
  const exact = flat.slice(i, i + a.length);
  const lineStart = plain.lastIndexOf("\n", i) + 1, lineEndRaw = plain.indexOf("\n", i), lineEnd = lineEndRaw < 0 ? plain.length : lineEndRaw;
  const line = flat.slice(lineStart, lineEnd), rel = i - lineStart;
  const starts = [0, ...[...line.matchAll(/[.!?]["')\]]?\s+(?=[A-Z0-9"'“])/g)].map(m => m.index + m[0].length)];
  const s = Math.max(...starts.filter(x => x <= rel));
  const nexts = starts.filter(x => x > rel + a.length - 1);
  const e = nexts.length ? Math.min(...nexts) : line.length;
  return {anchor: exact, sentence: line.slice(s, e).trim()};
}

// Splits Claude's link suggestions into verified links and ones whose anchor needs a retry.
// Links to URLs outside the sitemap, or to a URL already used, are dropped outright.
export function checkLinks(raw, plain, known, taken, headings = new Set()){
  const ok = [], bad = [];
  for (const l of raw){
    if (!l || !l.target_url || !l.anchor_text) continue;
    const t = normUrl(l.target_url);
    if (!known.has(t) || taken.urls.has(t)) continue;
    const hit = locateAnchor(plain, l.anchor_text, headings);
    if (!hit || taken.anchors.has(fold(hit.anchor))) { bad.push(l); continue; }
    taken.urls.add(t); taken.anchors.add(fold(hit.anchor));
    ok.push({...l, anchor_text: hit.anchor, sentence: hit.sentence, verified: true, on: true});
  }
  return {ok, bad};
}
