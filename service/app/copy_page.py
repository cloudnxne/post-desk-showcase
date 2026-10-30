"""A page that holds a post's HTML for copying: the Google Doc side panel can't copy to the clipboard, so it saves the
HTML here and opens this page in a browser tab, where a Copy button works. Links are unguessable and expire."""
from __future__ import annotations

import html
import json
import re
import secrets
import time

KEEP_DAYS = 7
MAX_CHARS = 500_000


def new_id() -> str:
    return secrets.token_urlsafe(18)


def valid_id(copy_id: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_-]{20,40}", copy_id or ""))


def key(copy_id: str) -> str:
    return f"copies/{copy_id}.json"


def record(content: str, title: str, user: str | None) -> str:
    return json.dumps({"html": content, "title": title, "user": user, "created": time.time()})


def fresh(rec: dict) -> bool:
    return time.time() - float(rec.get("created", 0)) < KEEP_DAYS * 86400


def page(rec: dict, nonce: str) -> str:
    title = html.escape(rec.get("title") or "Post")
    source = html.escape(rec["html"])
    return f"""<!doctype html>
<html lang="en-GB"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>HTML: {title}</title>
<style>
  body {{ font: 15px/1.5 Arial, sans-serif; color: #202124; margin: 0; background: #f8f9fa; }}
  main {{ max-width: 960px; margin: 0 auto; padding: 24px 16px; }}
  h1 {{ font-size: 20px; margin: 0 0 4px; }}
  p.note {{ color: #5f6368; margin: 0 0 16px; }}
  .bar {{ display: flex; gap: 8px; margin-bottom: 12px; flex-wrap: wrap; }}
  button {{ font: inherit; padding: 8px 16px; border-radius: 4px; border: 1px solid #dadce0; background: #fff; color: #1a73e8; cursor: pointer; }}
  button.primary {{ background: #1a73e8; color: #fff; border-color: #1a73e8; }}
  textarea {{ width: 100%; box-sizing: border-box; min-height: 60vh; font: 13px/1.5 Consolas, monospace; padding: 12px;
             border: 1px solid #dadce0; border-radius: 6px; background: #fff; }}
  iframe {{ width: 100%; min-height: 60vh; border: 1px solid #dadce0; border-radius: 6px; background: #fff; display: none; }}
  #done {{ color: #188038; align-self: center; }}
</style></head>
<body><main>
  <h1>{title}</h1>
  <p class="note">The post as clean HTML from Post Desk. Paste it into the website's HTML or code view. This page expires after {KEEP_DAYS} days.</p>
  <div class="bar">
    <button class="primary" id="copy">Copy HTML</button>
    <button id="toggle">Show preview</button>
    <span id="done"></span>
  </div>
  <textarea id="src" readonly>{source}</textarea>
  <iframe id="preview" sandbox="" title="Preview"></iframe>
</main>
<script nonce="{nonce}">
  const src = document.getElementById('src'), done = document.getElementById('done'), frame = document.getElementById('preview');
  document.getElementById('copy').addEventListener('click', async () => {{
    try {{ await navigator.clipboard.writeText(src.value); }}
    catch (e) {{ src.select(); document.execCommand('copy'); }}
    done.textContent = 'Copied';
    setTimeout(() => (done.textContent = ''), 2000);
  }});
  document.getElementById('toggle').addEventListener('click', (e) => {{
    const showing = frame.style.display === 'block';
    if (!showing) frame.srcdoc = '<!doctype html><meta charset="utf-8"><body style="font:16px/1.6 Arial,sans-serif;max-width:760px;margin:24px auto;padding:0 16px">' + src.value;
    frame.style.display = showing ? 'none' : 'block';
    src.style.display = showing ? 'block' : 'none';
    e.target.textContent = showing ? 'Show preview' : 'Show HTML';
  }});
</script>
</body></html>"""
