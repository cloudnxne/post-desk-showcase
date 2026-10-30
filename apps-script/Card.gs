/**
 * Post Desk as a Google Workspace side-panel add-on, so it shows in every Google Doc you open once installed
 * (Apps Script editor: Deploy > Test deployments > Install). It uses the same functions as the sidebar in Code.gs.
 *
 * Google limits these panels to its card layout and stops each button press after 30 seconds, so: the HTML opens
 * on its own page (from the API) with a working Copy button, and reading a site runs in one short step per click.
 */

const CARD_INDEX_SECONDS = 20;
const NO_DOC = 'Post Desk can\'t open this doc. Close and reopen the Post Desk panel and click Allow, and check you can edit the doc.';

// New permissions (like reading Search Console) need approving before anything else works: show Google's own
// authorise screen rather than a doc error.
function needsAuth_() {
  const info = ScriptApp.getAuthorizationInfo(ScriptApp.AuthMode.FULL);
  if (info.getAuthorizationStatus() === ScriptApp.AuthorizationStatus.REQUIRED) {
    CardService.newAuthorizationException()
      .setAuthorizationUrl(info.getAuthorizationUrl())
      .setResourceDisplayName('Post Desk')
      .throwException();
  }
}

function onDocsHomepage(e) {
  needsAuth_();
  return useDoc_(e) ? setupCard_() : allowCard_(e && e.docs && e.docs.id);
}

function onFileScopeGranted(e) {
  needsAuth_();
  return useDoc_(e) ? setupCard_() : allowCard_(true);
}

function allowCard_(tried) {
  const text = tried
    ? 'Post Desk can\'t open this doc yet. Click Allow below. If that doesn\'t fix it, check you can <b>edit</b> this doc ' +
      '(not just view or comment), then close and reopen the Post Desk panel.'
    : 'Post Desk needs your OK to read and add links to this doc.';
  const why = tried && DOC_ERROR_ ? '<br><br><font color="#5f6368">Google said: ' + esc_(DOC_ERROR_).slice(0, 300) + '</font>' : '';
  return CardService.newCardBuilder()
    .setHeader(CardService.newCardHeader().setTitle('Post Desk').setSubtitle('v' + SCRIPT_VERSION))
    .addSection(CardService.newCardSection()
      .addWidget(CardService.newTextParagraph().setText(text + why))
      .addWidget(CardService.newTextButton().setText('Allow for this doc')
        .setOnClickAction(CardService.newAction().setFunctionName('requestDocAccess'))))
    .build();
}

function requestDocAccess() {
  return CardService.newEditorFileScopeActionResponseBuilder().requestFileScopeForActiveDocument().build();
}

// ---------- helpers ----------
function input_(e, name) {
  const f = e && e.commonEventObject && e.commonEventObject.formInputs && e.commonEventObject.formInputs[name];
  return f && f.stringInputs ? f.stringInputs.value : [];
}

function form_(e) {
  return {
    // name, notes and priority stay undefined if the panel didn't send them, so the shared settings aren't wiped
    domain: input_(e, 'domain')[0] || '', name: input_(e, 'name')[0], notes: input_(e, 'notes')[0],
    priority: input_(e, 'priority')[0], keyword: input_(e, 'keyword')[0] || '', url: input_(e, 'url')[0] || '',
  };
}

function notify_(text) {
  return CardService.newActionResponseBuilder().setNotification(CardService.newNotification().setText(text)).build();
}

function push_(card) {
  return CardService.newActionResponseBuilder().setNavigation(CardService.newNavigation().pushCard(card)).build();
}

function button_(text, fn, filled) {
  const b = CardService.newTextButton().setText(text).setOnClickAction(CardService.newAction().setFunctionName(fn));
  return filled ? b.setTextButtonStyle(CardService.TextButtonStyle.FILLED) : b;
}

function saveResult_(r) {
  CacheService.getUserCache().put('result:' + doc_().getId(), JSON.stringify(r), 6 * 3600);
}

function lastResult_() {
  const raw = CacheService.getUserCache().get('result:' + doc_().getId());
  return raw ? JSON.parse(raw) : null;
}

function path_(u) {
  return String(u).replace(/^https?:\/\/[^/]+/, '') || '/';
}

function esc_(s) {
  return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

// ---------- the setup card ----------
function setupCard_(message) {
  const s = getSetup();
  const client = CardService.newSelectionInput().setType(CardService.SelectionInputType.DROPDOWN)
    .setTitle('Client').setFieldName('domain')
    .setOnChangeAction(CardService.newAction().setFunctionName('onClientChange'));
  client.addItem('Choose a client', '', !s.domain);
  s.clients.forEach(function (c) {
    client.addItem(c.domain + (c.indexed ? '' : ' (not read yet)'), c.domain, c.domain === s.domain);
  });

  const main = CardService.newCardSection();
  if (message || s.error) main.addWidget(CardService.newTextParagraph().setText('<b>' + esc_(message || s.error) + '</b>'));
  main.addWidget(client)
    .addWidget(CardService.newTextInput().setFieldName('newDomain').setTitle('Client not listed? Type their website')
      .setHint('e.g. example.co.uk'))
    .addWidget(button_('Add client', 'onAddClient'))
    .addWidget(CardService.newTextInput().setFieldName('keyword').setTitle('Target keyword (optional)').setValue(s.keyword))
    .addWidget(CardService.newTextInput().setFieldName('url').setTitle('Planned post URL (optional)').setValue(s.url))
    .addWidget(CardService.newButtonSet()
      .addButton(button_('Find links and metas', 'onRun', true))
      .addButton(button_('Copy as HTML', 'onHtml')));

  const picked = s.clients.filter(function (c) { return c.domain === s.domain; })[0];
  const card = CardService.newCardBuilder().setHeader(CardService.newCardHeader().setTitle('Post Desk').setSubtitle(s.title + ' · v' + SCRIPT_VERSION)).addSection(main);

  if (picked && !picked.indexed) {
    card.addSection(CardService.newCardSection()
      .addWidget(CardService.newTextParagraph().setText('Post Desk hasn\'t read this site yet. It works now using the sitemap, ' +
        'but links are better once the site has been read. Each click reads for about 20 seconds.'))
      .addWidget(button_('Read the client\'s site', 'onIndex')));
  }

  card.addSection(CardService.newCardSection().setHeader('Client settings (shared with the team)')
    .addWidget(CardService.newTextInput().setFieldName('name').setTitle('Client name').setValue(s.client.name || ''))
    .addWidget(CardService.newTextInput().setFieldName('notes').setTitle('Notes for the AI').setMultiline(true).setValue(s.client.notes || ''))
    .addWidget(CardService.newTextInput().setFieldName('priority').setTitle('Priority pages (one URL per line)').setMultiline(true)
      .setValue(s.client.priority || '')));
  return card.build();
}

function onAddClient(e) {
  if (!useDoc_(e)) return notify_(NO_DOC);
  try {
    const d = addClient(input_(e, 'newDomain')[0] || '');
    saveSettings_({ domain: d, keyword: input_(e, 'keyword')[0] || '', url: input_(e, 'url')[0] || '' });
    return CardService.newActionResponseBuilder().setNavigation(CardService.newNavigation()
      .updateCard(setupCard_('Added ' + d + '. Post Desk works for it now; read the site for better links.'))).build();
  } catch (err) {
    return notify_(err.message);
  }
}

function onClientChange(e) {
  if (!useDoc_(e)) return notify_(NO_DOC);
  const f = form_(e);
  const c = getClient(f.domain);
  saveSettings_({ domain: f.domain, keyword: f.keyword, url: f.url, name: c.name, notes: c.notes, priority: c.priority });
  return CardService.newActionResponseBuilder().setNavigation(CardService.newNavigation().updateCard(setupCard_())).build();
}

// ---------- actions ----------
function onRun(e) {
  if (!useDoc_(e)) return notify_(NO_DOC);
  const started = Date.now();
  let r;
  try {
    const form = form_(e);
    form.maxSeconds = 45;            // side-panel buttons can't wait forever, so ask Post Desk to finish in time
    r = runPostDesk(form);
    saveResult_(r);
    return push_(resultsCard_(r));
  } catch (err) {
    // Keep the reason on the panel (a pop-up note vanishes in seconds), and in the Executions log.
    const secs = Math.round((Date.now() - started) / 1000);
    console.error('Post Desk run failed after ' + secs + 's: ' + (err && err.stack || err));
    const msg = r ? 'The links came back but the results couldn\'t be shown: ' + err.message
      : 'Couldn\'t finish (' + secs + 's): ' + err.message;
    return CardService.newActionResponseBuilder().setNavigation(CardService.newNavigation().updateCard(setupCard_(msg))).build();
  }
}

function onIndex(e) {
  if (!useDoc_(e)) return notify_(NO_DOC);
  try {
    const s = indexSite(form_(e), 1, CARD_INDEX_SECONDS);
    const done = !s.remaining;
    const msg = done ? 'Done: ' + s.indexed + ' pages read. Links will now be picked by meaning.'
      : s.indexed + ' pages read so far, ' + s.remaining + ' to go. Click "Read the client\'s site" again.';
    return CardService.newActionResponseBuilder().setNavigation(CardService.newNavigation().updateCard(setupCard_(msg))).build();
  } catch (err) {
    return notify_(err.message);
  }
}

function onHtml(e) {
  if (!useDoc_(e)) return notify_(NO_DOC);
  const html = getHtml();
  if (!html) return notify_('The doc looks empty.');
  try {
    // The panel can't copy to the clipboard, so the HTML opens on its own page with a working Copy button.
    const saved = call_('post', '/copy', { html: html, title: doc_().getName() });
    return CardService.newActionResponseBuilder()
      .setOpenLink(CardService.newOpenLink().setUrl(saved.url).setOpenAs(CardService.OpenAs.FULL_SIZE))
      .build();
  } catch (err) {
    return notify_(err.message);
  }
}

function onInsert(e) {
  if (!useDoc_(e)) return notify_(NO_DOC);
  const r = lastResult_();
  if (!r) return notify_('Run Post Desk again first.');
  const picked = input_(e, 'links').map(function (i) { return r.internal_links[+i]; }).filter(Boolean);
  if (!picked.length) return notify_('Tick at least one link.');
  const o = insertLinks(picked);
  return notify_('Added ' + o.added.length + ' link' + (o.added.length === 1 ? '' : 's') + ' to the doc.' +
    (o.missing.length ? ' Couldn\'t find: ' + o.missing.join(', ') : ''));
}

function onNotes(e) {
  if (!useDoc_(e)) return notify_(NO_DOC);
  const r = lastResult_();
  if (!r) return notify_('Run Post Desk again first.');
  const i = +(input_(e, 'meta')[0] || 0);
  addSeoNotes(r.title_tag, r.meta_descriptions[i] ? r.meta_descriptions[i].text : '');
  return notify_('Added the title and meta description to the top of the doc.');
}

// ---------- the results card ----------
function resultsCard_(r) {
  const card = CardService.newCardBuilder().setHeader(CardService.newCardHeader().setTitle('Post Desk results')
    .setSubtitle(r.seconds + 's · ' + r.stats.verified + ' links checked'));

  card.addSection(CardService.newCardSection().setHeader('SEO title')
    .addWidget(CardService.newDecoratedText().setText(esc_(r.title_tag)).setWrapText(true)
      .setBottomLabel(r.title_tag.length + ' characters, ' + (r.title_status === 'good' ? 'fits' : 'may be cut off in Google'))));

  const metas = CardService.newSelectionInput().setType(CardService.SelectionInputType.RADIO_BUTTON).setFieldName('meta');
  const metaText = CardService.newCardSection().setHeader('Meta descriptions');
  r.meta_descriptions.forEach(function (m, i) {
    metaText.addWidget(CardService.newDecoratedText().setTopLabel('Option ' + (i + 1) + ' · ' + m.angle)
      .setText(esc_(m.text)).setWrapText(true)
      .setBottomLabel(m.length + ' characters, ' + (m.status === 'good' ? 'good length' : m.status)));
    metas.addItem('Use option ' + (i + 1), String(i), i === 0);
  });
  if (r.meta_descriptions.length) {
    metaText.addWidget(metas).addWidget(button_('Add title and chosen meta to the doc', 'onNotes'));
  }
  card.addSection(metaText);

  const linkSection = CardService.newCardSection().setHeader('Internal links');
  if (r.internal_links.length) {
    const boxes = CardService.newSelectionInput().setType(CardService.SelectionInputType.CHECK_BOX).setFieldName('links');
    r.internal_links.forEach(function (l, i) {
      boxes.addItem('"' + l.anchor_text + '" → ' + path_(l.target_url), String(i), true);
    });
    linkSection.addWidget(boxes).addWidget(CardService.newButtonSet()
      .addButton(button_('Add ticked links to the doc', 'onInsert', true))
      .addButton(button_('Copy as HTML', 'onHtml')));
  } else {
    linkSection.addWidget(CardService.newTextParagraph().setText('No links fitted this post.'));
  }
  card.addSection(linkSection);

  if (r.link_back_from.length) {
    const back = CardService.newCardSection().setHeader('Pages that should link to this post');
    r.link_back_from.forEach(function (b) {
      back.addWidget(CardService.newDecoratedText().setText(esc_(path_(b.source_url))).setWrapText(true)
        .setBottomLabel('Anchor: "' + b.anchor_text + '"'));
    });
    card.addSection(back);
  }

  const sc = r.stats.search_console;
  const notes = [];
  if (sc && !sc.error) notes.push('Search Console data used.');
  else if (sc && /no_property/.test(sc.error)) notes.push('No Search Console data for this client.');
  if (r.stats.retrieval === 'url') notes.push('This site hasn\'t been read yet, so links came from page addresses.');
  if (r.stats.fell_back) notes.push('Backup model used.');
  if (notes.length) card.addSection(CardService.newCardSection().addWidget(CardService.newTextParagraph().setText(notes.join(' '))));
  return card.build();
}
