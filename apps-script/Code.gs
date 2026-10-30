/**
 * Post Desk for Google Docs.
 *
 * Adds a "Post Desk" menu to the doc. The sidebar sends the post to the Post Desk API, signed in as the person
 * using it (their Google sign-in, so only accounts in your Google Workspace domain get in), then shows the title tag, meta descriptions
 * and internal links. Ticked links are added to the doc as real hyperlinks.
 *
 * Setup: docs/DEPLOY.md, "Google Doc front end".
 */

// Your Post Desk API address: set it once in the Apps Script editor under Project Settings > Script properties,
// as POST_DESK_API_URL (for example https://post-desk-api-xxxxxxxx-nw.a.run.app).
function apiUrl_() {
  const url = PropertiesService.getScriptProperties().getProperty('POST_DESK_API_URL');
  if (!url) throw new Error('Post Desk isn\'t set up yet: add the script property POST_DESK_API_URL (Project Settings > Script properties).');
  return url.replace(/\/+$/, '');
}
const SCRIPT_VERSION = '2.0';      // shown in the panel and in Check sign-in, so you can tell which code is running
const INDEX_STEP_SECONDS = 45;     // each indexing call stays well inside Apps Script's time limits
const INDEX_MAX_STEPS = 5;

// ---------- which doc ----------
// In the doc's own menu this is simply the open doc. In the side-panel add-on (Card.gs) the doc comes from the
// add-on event, so Card.gs sets it with useDoc_ before calling anything here.
let DOC_ = null;
let DOC_ERROR_ = '';

// Run this once from the Apps Script editor (choose "authorize" and click Run) to approve any new permissions.
function authorize() {
  return 'Post Desk is authorised (script version ' + SCRIPT_VERSION + ').';
}

function doc_() {
  return DOC_ || DocumentApp.getActiveDocument();
}

// The side panel reads and edits the doc through the Google Docs API, which honours the per-doc "Allow" grant.
// (DocumentApp can't open other docs from a side panel, and its "active document" can be the doc the script lives in.)
// Returns null, with the reason in DOC_ERROR_, if Google won't give access, so the panel can ask again.
function useDoc_(e) {
  const id = e && e.docs && e.docs.id;
  if (!id) return null;
  const res = UrlFetchApp.fetch('https://docs.googleapis.com/v1/documents/' + encodeURIComponent(id), {
    muteHttpExceptions: true, headers: { Authorization: 'Bearer ' + ScriptApp.getOAuthToken() },
  });
  if (res.getResponseCode() !== 200) {
    let msg = res.getContentText().slice(0, 300);
    try { msg = JSON.parse(res.getContentText()).error.message; } catch (err) { /* keep the raw text */ }
    DOC_ERROR_ = msg + ' (error ' + res.getResponseCode() + ')';
    DOC_ = null;
    return null;
  }
  DOC_ = restDoc_(JSON.parse(res.getContentText()));
  return DOC_;
}

// Per-doc settings (client, keyword, planned URL), kept in the user's own settings under the doc's ID,
// so they work the same from the menu and from the side panel.
function docSettings_() {
  const raw = PropertiesService.getUserProperties().getProperty('doc:' + doc_().getId());
  return raw ? JSON.parse(raw) : {};
}

// ---------- menu and sidebar ----------
function onOpen() {
  DocumentApp.getUi().createMenu('Post Desk')
    .addItem('Open Post Desk', 'showSidebar')
    .addItem('Check sign-in', 'showSignInCheck')
    .addToUi();
}

function onInstall(e) {
  onOpen(e);
}

function showSidebar() {
  DocumentApp.getUi().showSidebar(HtmlService.createHtmlOutputFromFile('Sidebar').setTitle('Post Desk'));
}

// ---------- calling the API ----------
function call_(method, path, payload) {
  const options = {
    method: method,
    muteHttpExceptions: true,
    headers: {
      Authorization: 'Bearer ' + ScriptApp.getIdentityToken(),
      // Lets Post Desk read this person's own Search Console for this request (never stored).
      'X-Google-Access-Token': ScriptApp.getOAuthToken(),
    },
  };
  if (payload) {
    options.contentType = 'application/json';
    options.payload = JSON.stringify(payload);
  }
  const res = UrlFetchApp.fetch(apiUrl_() + path, options);
  const code = res.getResponseCode();
  let data;
  try {
    data = JSON.parse(res.getContentText());
  } catch (e) {
    data = { detail: res.getContentText().slice(0, 300) };
  }
  if (code >= 400) {
    const d = data.detail;
    const message = typeof d === 'string' ? d : (d && d.message) || JSON.stringify(d);
    throw new Error(friendly_(code, message));
  }
  return data;
}

function friendly_(code, message) {
  if (code === 401) {
    return 'Post Desk didn\'t accept your sign-in (' + message + '). If this is your first time, use Post Desk > Check sign-in and send the result to whoever set up Post Desk.';
  }
  if (code === 403) return message;
  if (code === 429) return 'The AI model is busy right now. Please try again in a minute.';
  if (code === 504) {
    const where = (message.match(/Reading the site[^]*$/) || [''])[0];
    return 'The AI model took too long to answer. Please try again. ' + where;
  }
  return message + ' (error ' + code + ')';
}

function tokenDetails_() {
  const part = ScriptApp.getIdentityToken().split('.')[1];
  const padded = part + '==='.slice((part.length + 3) % 4);
  return JSON.parse(Utilities.newBlob(Utilities.base64DecodeWebSafe(padded)).getDataAsString());
}

function showSignInCheck() {
  const ui = DocumentApp.getUi();
  const t = tokenDetails_();
  let result;
  try {
    call_('get', '/clients');
    result = 'Post Desk accepted your sign-in. You\'re all set.';
  } catch (e) {
    result = 'Not accepted yet: ' + e.message;
  }
  ui.alert('Post Desk sign-in',
    'Post Desk script version ' + SCRIPT_VERSION + '\n\nAccount: ' + t.email + '\nWorkspace: ' + (t.hd || '(none, so this is a personal account)') +
    '\nScript client ID: ' + t.aud + '\n\n' + result, ui.ButtonSet.OK);
}

// ---------- settings ----------
// The doc remembers its client, keyword and planned URL. Client notes and priority pages are shared by everyone
// who uses this script, so a client only needs setting up once.
function getSetup() {
  const saved = docSettings_();
  const domain = saved.domain || '';
  let clients = [];
  let error = null;
  try {
    const got = call_('get', '/clients');       // indexed sites plus your Search Console properties
    clients = got.clients;
    if (got.problems) error = got.problems.join(' ');
  } catch (e) {
    error = e.message;
  }
  try {                                         // plus clients anyone added by hand
    addedClients_().forEach(function (d) {
      if (!clients.some(function (c) { return c.domain === d; })) clients.push({ domain: d, indexed: false, search_console: false });
    });
    clients.sort(function (a, b) { return a.domain < b.domain ? -1 : 1; });
  } catch (e) {
    error = e.message;
  }
  return {
    clients: clients,
    error: error,
    domain: domain,
    keyword: saved.keyword || '',
    url: saved.url || '',
    client: getClient(domain),
    title: doc_().getName(),
    version: SCRIPT_VERSION,
  };
}

// ---------- clients added by hand (shared with everyone using the script) ----------
function cleanDomain_(d) {
  return String(d || '').trim().toLowerCase().replace(/^[a-z]+:\/\//, '').split('/')[0].split(':')[0].replace(/^www\./, '');
}

function addedClients_() {
  const raw = PropertiesService.getScriptProperties().getProperty('clients:added');
  return raw ? JSON.parse(raw) : [];
}

function addClient(domain) {
  const d = cleanDomain_(domain);
  if (!/^[a-z0-9-]+(\.[a-z0-9-]+)+$/.test(d)) throw new Error('That doesn\'t look like a website address, e.g. example.co.uk');
  const list = addedClients_();
  if (list.indexOf(d) < 0) {
    list.push(d);
    PropertiesService.getScriptProperties().setProperty('clients:added', JSON.stringify(list));
  }
  return d;
}

function getClient(domain) {
  const raw = domain && PropertiesService.getScriptProperties().getProperty('client:' + domain);
  return raw ? JSON.parse(raw) : { name: '', notes: '', priority: '' };
}

function saveSettings_(form) {
  PropertiesService.getUserProperties().setProperty('doc:' + doc_().getId(), JSON.stringify({
    domain: form.domain, keyword: form.keyword || '', url: form.url || '',
  }));
  const old = getClient(form.domain);
  const pick = function (k) { return form[k] === undefined || form[k] === null ? old[k] || '' : form[k]; };
  PropertiesService.getScriptProperties().setProperty('client:' + form.domain, JSON.stringify({
    name: pick('name'), notes: pick('notes'), priority: pick('priority'),
  }));
}

// ---------- the post ----------
// Sends the doc as Markdown, so the API can tell headings and lists from body text
// (link anchors are never taken from headings).
function postText_() {
  const body = doc_().getBody();
  const H = DocumentApp.ParagraphHeading;
  const level = {};
  level[H.TITLE] = 1; level[H.HEADING1] = 1; level[H.HEADING2] = 2; level[H.HEADING3] = 3;
  level[H.HEADING4] = 4; level[H.HEADING5] = 5; level[H.HEADING6] = 6;
  const lines = [];
  for (let i = 0; i < body.getNumChildren(); i++) {
    const el = body.getChild(i);
    const type = el.getType();
    const text = el.asText ? el.asText().getText().trim() : '';
    if (!text || /^(SEO title|Meta description): /.test(text)) continue;   // skip the notes Post Desk adds
    if (type === DocumentApp.ElementType.PARAGRAPH) {
      const h = level[el.asParagraph().getHeading()];
      lines.push(h ? '#'.repeat(h) + ' ' + text : text);
    } else if (type === DocumentApp.ElementType.LIST_ITEM) {
      lines.push('- ' + text);
    } else {
      lines.push(text);          // tables and anything else: just the words
    }
    lines.push('');
  }
  return lines.join('\n').trim();
}

function runPostDesk(form) {
  if (!form.domain) throw new Error('Pick a client first.');
  form.domain = addClient(form.domain);         // a newly typed client is remembered for everyone
  saveSettings_(form);
  const content = postText_();
  const words = content.split(/\s+/).filter(String).length;
  if (words < 40) throw new Error('This doc only has ' + words + ' words. Write or paste the post first.');
  const priority = (getClient(form.domain).priority || '').split(/\s+/).filter(function (u) { return /^https?:\/\//i.test(u); });
  const started = Date.now();
  const saved = getClient(form.domain);
  const result = call_('post', '/generate', {
    client: { name: saved.name || form.domain, domain: form.domain, notes: saved.notes || '', priority_pages: priority },
    post: { title: doc_().getName(), keyword: form.keyword || '', url: form.url || '', content: content },
    sitemap_urls: [],
    options: form.maxSeconds ? { max_seconds: form.maxSeconds } : {},
  });
  result.seconds = Math.round((Date.now() - started) / 1000);
  try {   // kept for six hours, so Copy as HTML can use the headings and lists Claude spotted
    CacheService.getUserCache().put('result:' + doc_().getId(), JSON.stringify(result), 6 * 3600);
  } catch (e) { /* too big for the cache: Copy as HTML falls back to the doc's own formatting */ }
  return result;
}

// ---------- adding links to the doc ----------
function insertLinks(links) {
  const body = doc_().getBody();
  const out = { added: [], missing: [] };
  links.forEach(function (l) {
    (linkOne_(body, l) ? out.added : out.missing).push(l.anchor_text);
  });
  if (doc_().flush) doc_().flush();            // the side panel's Docs API version sends its changes in one go
  return out;
}

function linkOne_(body, link) {
  const pattern = link.anchor_text.replace(/[.*+?^${}()|[\]\\\/]/g, '\\$&');
  const sentenceStart = (link.sentence || '').slice(0, 60);
  let fallback = null;
  let found = body.findText(pattern);
  while (found) {
    const text = found.getElement().asText();
    const start = found.getStartOffset();
    const end = found.getEndOffsetInclusive();
    const para = paragraphOf_(text);
    const isHeading = para && para.getHeading() !== DocumentApp.ParagraphHeading.NORMAL;
    if (text.getText().substring(start, end + 1) === link.anchor_text && !isHeading && !text.getLinkUrl(start)) {
      if (!sentenceStart || (para && para.getText().indexOf(sentenceStart) >= 0)) {
        text.setLinkUrl(start, end, link.target_url);
        return true;
      }
      fallback = fallback || { text: text, start: start, end: end };
    }
    found = body.findText(pattern, found);
  }
  if (fallback) {
    fallback.text.setLinkUrl(fallback.start, fallback.end, link.target_url);
    return true;
  }
  return false;
}

function paragraphOf_(el) {
  let p = el.getParent();
  while (p && p.getType() !== DocumentApp.ElementType.PARAGRAPH && p.getType() !== DocumentApp.ElementType.LIST_ITEM) {
    p = p.getParent();
  }
  return p;
}

// ---------- the post as clean HTML ----------
// SEO-structured HTML for the website, built like the Post Desk page does it:
//  - the post title and anything above it (briefs, notes) are left out, as the CMS adds the H1 from the title;
//  - headings from the doc's heading styles, else the headings Claude spotted in the last run, else a short line
//    that's all bold; levels start at H2 and never skip;
//  - lists from real Doc lists, typed bullets or numbers, or lines Claude spotted as list items;
//  - links, bold and italic kept; "Label: explanation" list items get the label in bold.
const MARKER_UL = /^(?:[-*+]\s+|[•●▪◦‣►▸➤→✓✔☐☑◆■□–—]\s*|o\s+(?=[A-Z0-9]))/;
const MARKER_OL = /^(?:\d{1,2}[.)]|\(\d{1,2}\)|[a-h][.)])\s+/;
const MARKER_HTML = /^\s*(?:[-*+]\s+|[•●▪◦‣►▸➤→✓✔☐☑◆■□–—]\s*|\d{1,2}[.)]\s+|\(\d{1,2}\)\s+|[a-h][.)]\s+)/;

function key_(t) {
  return String(t || '').replace(/^#{1,6}\s+/, '').replace(/[‘’]/g, "'").replace(/[“”]/g, '"')
    .replace(/[:\s]+$/, '').replace(/\s+/g, ' ').trim().toLowerCase();
}

function stripMarker_(t) {
  t = String(t).trim();
  if (MARKER_OL.test(t)) return { type: 'ol', text: t.replace(MARKER_OL, '') };
  if (MARKER_UL.test(t)) return { type: 'ul', text: t.replace(MARKER_UL, '') };
  return { type: null, text: t };
}

function cachedResult_() {
  try {
    const raw = CacheService.getUserCache().get('result:' + doc_().getId());
    return raw ? JSON.parse(raw) : null;
  } catch (e) {
    return null;
  }
}

function getHtml() {
  const hints = cachedResult_() || {};
  const hmap = {};
  (hints.headings || []).forEach(function (h) { hmap[key_(h.text)] = Math.min(4, Math.max(1, +h.level || 2)); });
  const lmap = {};
  (hints.lists || []).forEach(function (l) {
    (l.items || []).forEach(function (it) { lmap[key_(stripMarker_(it).text)] = l.type === 'ol' ? 'ol' : 'ul'; });
  });
  const titleKey = key_(doc_().getName());

  // 1. The doc as blocks: headings, paragraphs, list items and tables.
  const body = doc_().getBody();
  const E = DocumentApp.ElementType;
  const H = DocumentApp.ParagraphHeading;
  const styled = {};
  styled[H.HEADING1] = 1; styled[H.HEADING2] = 2; styled[H.HEADING3] = 3;
  styled[H.HEADING4] = 4; styled[H.HEADING5] = 4; styled[H.HEADING6] = 4;
  const ORDERED = [DocumentApp.GlyphType.NUMBER, DocumentApp.GlyphType.LATIN_LOWER, DocumentApp.GlyphType.LATIN_UPPER,
    DocumentApp.GlyphType.ROMAN_LOWER, DocumentApp.GlyphType.ROMAN_UPPER];
  const blocks = [];
  for (let i = 0; i < body.getNumChildren(); i++) {
    const el = body.getChild(i);
    const type = el.getType();
    const text = el.asText ? el.asText().getText().replace(/\u000b/g, ' ').trim() : '';
    if (!text || /^(SEO title|Meta description): /.test(text)) continue;
    if (type === E.TABLE) {
      const table = el.asTable();
      const rows = [];
      for (let r = 0; r < table.getNumRows(); r++) {
        const row = table.getRow(r);
        const cells = [];
        for (let c = 0; c < row.getNumCells(); c++) cells.push('<td>' + escHtml_(row.getCell(c).getText().trim()) + '</td>');
        rows.push('  <tr>' + cells.join('') + '</tr>');
      }
      blocks.push({ raw: '<table>\n' + rows.join('\n') + '\n</table>', text: text });
      continue;
    }
    const inline = inlineHtml_(el);
    if (type === E.LIST_ITEM) {
      const item = el.asListItem();
      blocks.push({ li: ORDERED.indexOf(item.getGlyphType()) >= 0 ? 'ol' : 'ul', html: inline.html, text: text });
      continue;
    }
    if (type !== E.PARAGRAPH) continue;
    const style = el.asParagraph().getHeading();
    if (style === H.TITLE || style === H.SUBTITLE) {
      blocks.push({ front: true, text: text });   // doc template: client name and the consultant / copywriter line
      continue;
    }
    let level = styled[el.asParagraph().getHeading()] || hmap[key_(text)] || 0;
    let guess = false;
    const words = text.split(/\s+/).length;
    const isQ = /^Q[.:)]\s+\S/.test(text) && words <= 30;             // "Q. Can you lay a patio...?"
    if (!level && (isQ || (inline.allBold && text.length <= 100 && words <= 15 && !/[.!,;]$/.test(text)))) {
      level = 2;                                                        // bold lines and questions count
      guess = true;
    }
    if (!level && key_(text) === titleKey) level = 1;
    if (level) {
      blocks.push({ h: level, guess: guess, question: /\?$/.test(text), text: text.replace(/^Q[.:)]\s+/, ''),
                    html: inline.html.replace(/^((?:<[^>]+>)*)Q[.:)]\s+/, '$1') });
      continue;
    }
    if (/^A[.:)]\s+\S/.test(text)) {                                   // "A. While it is possible..."
      blocks.push({ p: true, html: inline.html.replace(/^((?:<[^>]+>)*)A[.:)]\s+/, '$1'), text: text });
      continue;
    }
    const m = stripMarker_(text);
    const lt = m.type || lmap[key_(m.text)];
    if (lt) { blocks.push({ li: lt, html: inline.html.replace(MARKER_HTML, ''), text: m.text }); continue; }
    blocks.push({ p: true, html: inline.html, text: text });
  }

  // 2. Drop the title and anything above it.
  // The title is the line matching the doc's name; or else the first heading, if it's an H1 or only short lines
  // (like "IM Consultant: ... Copywriter: ...") come before it. If none of these, nothing is dropped.
  let titleAt = -1;
  for (let i = 0; i < blocks.length; i++) {
    if (!blocks[i].front && key_(blocks[i].text) === titleKey) { titleAt = i; break; }
  }
  if (titleAt < 0) {          // a Heading 1 (or a line Claude marked as the title) before any proper paragraph
    for (let i = 0; i < blocks.length; i++) {
      const b = blocks[i];
      if (b.h === 1 && !b.guess) { titleAt = i; break; }
      if (!(b.front || (b.p && b.text.split(/\s+/).length <= 25))) break;   // only front matter or short brief lines above
    }
  }
  if (titleAt < 0) {
    for (let i = 0; i < blocks.length; i++) {
      if (!blocks[i].h) continue;
      const onlyShortAbove = i <= 6 && blocks.slice(0, i).every(function (b) { return b.front || (!b.li && b.text.split(/\s+/).length <= 25); });
      if (onlyShortAbove) titleAt = i;
      break;
    }
  }
  // Still nothing? A plain-text title: the last short line (3 to 20 words, no full stop) before the first proper
  // paragraph, near the top. Lines above it, like "IM Consultant: ... Copywriter: ...", go with it.
  if (titleAt < 0) {
    const firstPara = blocks.findIndex(function (b) { return b.p && b.text.split(/\s+/).length > 25; });
    for (let i = Math.min(firstPara, 7) - 1; i >= 0; i--) {
      const n = blocks[i].text.split(/\s+/).length;
      if (!blocks[i].front && !blocks[i].li && !blocks[i].raw && n >= 3 && n <= 20 && !/[.!?]$/.test(blocks[i].text)) { titleAt = i; break; }
    }
  }
  const kept = (titleAt >= 0 ? blocks.slice(titleAt + 1) : blocks).filter(function (b) { return !b.front; });

  // 3. Fix heading levels (H2 first, none skipped) and write the HTML.
  const out = [];
  let prev = 1;
  let faq = 0;               // level of an FAQ heading we're under: its questions go one level below it
  let list = null;
  kept.forEach(function (b) {
    if (b.li) {
      if (!list || list.type !== b.li) { list = { type: b.li, items: [] }; out.push(list); }
      list.items.push(/<strong>/.test(b.html) ? b.html : b.html.replace(/^([^:<>]{2,40}):\s+(?=\S)/, function (m, label) {
        return /^https?$/i.test(label) ? m : '<strong>' + label + ':</strong> ';
      }));
      return;
    }
    list = null;
    if (b.raw) { out.push(b.raw); return; }
    if (b.h) {
      let h = Math.max(2, Math.min(b.h, prev + 1));
      if (b.guess && b.question && faq) {
        h = faq + 1;
      } else {
        faq = /\b(faqs?|frequently asked questions|common questions)\b/i.test(b.text) ? h : 0;
      }
      prev = h;
      out.push('<h' + h + '>' + b.html.replace(/<\/?(strong|em)>/g, '') + '</h' + h + '>');
      return;
    }
    out.push('<p>' + b.html + '</p>');
  });
  return out.map(function (b) {
    return typeof b === 'string' ? b
      : '<' + b.type + '>\n' + b.items.map(function (i) { return '  <li>' + i + '</li>'; }).join('\n') + '\n</' + b.type + '>';
  }).join('\n\n');
}

function inlineHtml_(container) {
  let html = '';
  let allBold = true;
  let any = false;
  for (let i = 0; i < container.getNumChildren(); i++) {
    const child = container.getChild(i);
    if (child.getType() !== DocumentApp.ElementType.TEXT) continue;
    const r = textRunsHtml_(child.asText());
    html += r.html;
    allBold = allBold && r.allBold;
    any = any || r.any;
  }
  // trim spaces at either end, including ones sitting just inside a closing or opening tag ("important? </strong>")
  html = html.trim().replace(/\s+((?:<\/[^>]+>)+)$/, '$1').replace(/^((?:<[^/>][^>]*>)+)\s+/, '$1');
  return { html: html, allBold: any && allBold };
}

// Splits a text element where its formatting changes, and wraps each run in strong, em and a tags.
function textRunsHtml_(text) {
  const s = text.getText();
  const starts = text.getTextAttributeIndices();
  const parts = [];                       // [link url or null, html], merged when the link is the same
  let allBold = true;
  let any = false;
  for (let k = 0; k < starts.length; k++) {
    const from = starts[k];
    const to = k + 1 < starts.length ? starts[k + 1] : s.length;
    if (from >= to) continue;
    const piece = s.substring(from, to);
    const bold = !!text.isBold(from);
    if (piece.trim()) { any = true; allBold = allBold && bold; }
    let run = escHtml_(piece);
    if (text.isItalic(from)) run = '<em>' + run + '</em>';
    if (bold) run = '<strong>' + run + '</strong>';
    const url = text.getLinkUrl(from) || null;
    const last = parts[parts.length - 1];
    if (last && last[0] === url) last[1] += run; else parts.push([url, run]);
  }
  const html = parts.map(function (p) { return p[0] ? '<a href="' + escHtml_(p[0]) + '">' + p[1] + '</a>' : p[1]; }).join('')
    .replace(/<\/strong><strong>/g, '').replace(/<\/em><em>/g, '');
  return { html: html, allBold: allBold, any: any };
}

function escHtml_(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;')
    .replace(/\u000b/g, '<br>');
}

function addSeoNotes(title, meta) {
  const lines = ['SEO title: ' + title, 'Meta description: ' + meta];
  if (doc_().insertNotes) return doc_().insertNotes(lines);
  const body = doc_().getBody();
  for (let i = lines.length - 1; i >= 0; i--) {
    const p = body.insertParagraph(0, lines[i]);
    p.setHeading(DocumentApp.ParagraphHeading.NORMAL);
    p.editAsText().setFontSize(9).setForegroundColor('#5f6368').setItalic(true);
  }
  return true;
}

// ---------- reading a new client's site ----------
function indexSite(form, steps, seconds) {
  if (!form.domain) throw new Error('Give the client\'s website.');
  form.domain = addClient(form.domain);
  saveSettings_(form);
  let stats = null;
  for (let step = 0; step < (steps || INDEX_MAX_STEPS); step++) {
    stats = call_('post', '/index', {
      client: { name: form.name || form.domain, domain: form.domain },
      sitemap_url: form.sitemap || '',        // found automatically when left empty
      budget_s: seconds || INDEX_STEP_SECONDS,
    });
    if (!stats.remaining || stats.stopped_early) break;
  }
  return stats;
}


// ---------- the doc through the Google Docs API (side panel) ----------
// Wraps the Docs API's JSON in the few DocumentApp-style calls the code above uses (getBody, getChild, findText,
// isBold, setLinkUrl...), so posts, links and HTML work the same in the side panel and the doc's own sidebar.
// Link changes are queued and sent in one batchUpdate by flush().
function restDoc_(json) {
  const E = DocumentApp.ElementType;
  const H = DocumentApp.ParagraphHeading;
  const G = DocumentApp.GlyphType;
  const STYLE = { TITLE: H.TITLE, SUBTITLE: H.SUBTITLE, HEADING_1: H.HEADING1, HEADING_2: H.HEADING2, HEADING_3: H.HEADING3,
                  HEADING_4: H.HEADING4, HEADING_5: H.HEADING5, HEADING_6: H.HEADING6 };
  const ORDERED = ['DECIMAL', 'ZERO_DECIMAL', 'ALPHA', 'UPPER_ALPHA', 'ROMAN', 'UPPER_ROMAN'];
  const requests = [];

  function textOf(paragraph) {
    // Text runs in order, with where each sits in the doc (indices), so a match in the text maps back to the doc.
    const runs = [];
    let text = '';
    (paragraph.elements || []).forEach(function (el) {
      if (!el.textRun) return;                               // images and the like take a doc index but no text
      const content = el.textRun.content || '';
      runs.push({ from: text.length, len: content.length, doc: el.startIndex, style: el.textRun.textStyle || {} });
      text += content;
    });
    if (text.slice(-1) === '\n') text = text.slice(0, -1);
    return { text: text, runs: runs };
  }

  function makeParagraph(se) {
    const p = se.paragraph;
    const t = textOf(p);
    const links = {};                                         // links set during this run
    const runAt = function (i) {
      for (let k = t.runs.length - 1; k >= 0; k--) if (i >= t.runs[k].from) return t.runs[k];
      return null;
    };
    const docIndex = function (i) { const r = runAt(i); return r ? r.doc + (i - r.from) : null; };
    const para = {};
    const text = {
      getType: function () { return E.TEXT; },
      asText: function () { return text; },
      getText: function () { return t.text; },
      getParent: function () { return para; },
      getTextAttributeIndices: function () {
        return t.runs.map(function (r) { return r.from; }).filter(function (i) { return i < t.text.length; });
      },
      isBold: function (i) { const r = runAt(i); return !!(r && r.style.bold); },
      isItalic: function (i) { const r = runAt(i); return !!(r && r.style.italic); },
      getLinkUrl: function (i) {
        if (links[i]) return links[i];
        const r = runAt(i);
        return r && r.style.link && r.style.link.url ? r.style.link.url : null;
      },
      setLinkUrl: function (s, e, url) {
        links[s] = url;
        requests.push({ updateTextStyle: { range: { startIndex: docIndex(s), endIndex: docIndex(e) + 1 },
                                           textStyle: { link: { url: url } }, fields: 'link' } });
      },
    };
    const bullet = p.bullet;
    const list = bullet && json.lists && json.lists[bullet.listId];
    const level = list && list.listProperties && list.listProperties.nestingLevels &&
      list.listProperties.nestingLevels[bullet.nestingLevel || 0];
    Object.assign(para, {
      getType: function () { return bullet ? E.LIST_ITEM : E.PARAGRAPH; },
      asText: function () { return text; },
      asParagraph: function () { return para; },
      asListItem: function () { return para; },
      getText: function () { return t.text; },
      getHeading: function () { return STYLE[(p.paragraphStyle || {}).namedStyleType] || H.NORMAL; },
      getListId: function () { return bullet ? bullet.listId : null; },
      getGlyphType: function () { return level && ORDERED.indexOf(level.glyphType) >= 0 ? G.NUMBER : G.BULLET; },
      getNumChildren: function () { return 1; },
      getChild: function () { return text; },
      _text: text,
    });
    return para;
  }

  function makeTable(se) {
    const rows = (se.table.tableRows || []).map(function (row) {
      return (row.tableCells || []).map(function (cell) {
        return (cell.content || []).filter(function (c) { return c.paragraph; })
          .map(function (c) { return textOf(c.paragraph).text; }).join(' ');
      });
    });
    const all = rows.map(function (r) { return r.join(' '); }).join(' ');
    const table = {
      getType: function () { return E.TABLE; },
      asText: function () { return { getText: function () { return all; } }; },
      asTable: function () { return table; },
      getNumRows: function () { return rows.length; },
      getRow: function (r) {
        return { getNumCells: function () { return rows[r].length; },
                 getCell: function (c) { return { getText: function () { return rows[r][c]; } }; } };
      },
    };
    return table;
  }

  const kids = ((json.body && json.body.content) || []).map(function (se) {
    return se.paragraph ? makeParagraph(se) : se.table ? makeTable(se) : null;
  }).filter(Boolean);
  const paragraphs = kids.filter(function (k) { return k._text; });

  const body = {
    getNumChildren: function () { return kids.length; },
    getChild: function (i) { return kids[i]; },
    findText: function (pattern, from) {
      const re = new RegExp(pattern, 'gi');
      let k = from ? from._k : 0;
      let start = from ? from.getEndOffsetInclusive() + 1 : 0;
      for (; k < paragraphs.length; k++, start = 0) {
        re.lastIndex = start;
        const m = re.exec(paragraphs[k].getText());
        if (m) {
          const kk = k, at = m.index, end = m.index + m[0].length - 1;
          return { _k: kk, getElement: function () { return paragraphs[kk]._text; },
                   getStartOffset: function () { return at; }, getEndOffsetInclusive: function () { return end; } };
        }
      }
      return null;
    },
  };

  function send() {
    if (!requests.length) return;
    const res = UrlFetchApp.fetch('https://docs.googleapis.com/v1/documents/' + encodeURIComponent(json.documentId) + ':batchUpdate', {
      method: 'post', contentType: 'application/json', muteHttpExceptions: true,
      headers: { Authorization: 'Bearer ' + ScriptApp.getOAuthToken() },
      payload: JSON.stringify({ requests: requests.splice(0) }),
    });
    if (res.getResponseCode() !== 200) throw new Error('Google Docs didn\'t accept the change (error ' + res.getResponseCode() + ').');
  }

  return {
    getId: function () { return json.documentId; },
    getName: function () { return json.title || ''; },
    getBody: function () { return body; },
    flush: send,
    insertNotes: function (lines) {
      const text = lines.join('\n') + '\n';
      requests.push({ insertText: { location: { index: 1 }, text: text } });
      requests.push({ updateParagraphStyle: { range: { startIndex: 1, endIndex: 1 + text.length },
                                              paragraphStyle: { namedStyleType: 'NORMAL_TEXT' }, fields: 'namedStyleType' } });
      requests.push({ updateTextStyle: { range: { startIndex: 1, endIndex: text.length },
                                         textStyle: { italic: true, bold: false, fontSize: { magnitude: 9, unit: 'PT' },
                                                      foregroundColor: { color: { rgbColor: { red: 0.37, green: 0.39, blue: 0.41 } } } },
                                         fields: 'italic,bold,fontSize,foregroundColor' } });
      send();
      return true;
    },
  };
}
