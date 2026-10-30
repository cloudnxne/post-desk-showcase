"""Runs the same post through each model and effort level on the live API, and prints a side-by-side comparison.

Run in Cloud Shell (it reads the API key from Secret Manager):
    curl -sO https://raw.githubusercontent.com/cloudnxne/post-desk/main/service/scripts/compare_models.py
    python3 compare_models.py --domain lawnco.example.co.uk --sitemap https://lawnco.example.co.uk/sitemap_index.xml

Options:
    --post FILE          the post to test (plain text or Markdown); a sample artificial grass post if left out
    --providers LIST     comma-separated, default anthropic,gemini (claude-vertex also works once enabled)
    --efforts LIST       comma-separated, default low,medium
    --runs N             runs per combination, default 1 (more runs give fairer timings)

Each run is a real model call, so it costs a little each time (pennies for the default 4 runs).
"""
import argparse
import json
import re
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request

import os

# Set these for your own setup, or pass --url and --project.
URL = os.environ.get("POST_DESK_URL", "")
PROJECT = os.environ.get("GOOGLE_CLOUD_PROJECT", "")
SAMPLE = """How to keep artificial grass looking new

Artificial grass is low maintenance, but it still needs a little care. Brush the fibres against the grain every few weeks so they stay upright, especially in high traffic areas.

If you have pets, rinse the lawn regularly and use a pet-safe artificial grass cleaner to stop odours building up.

A good installation makes the biggest difference. A properly compacted sub-base and a weed membrane stop dips and weeds coming through for years.

Thinking about a new lawn? Get a free quote and we will check your garden first."""


def sitemap_urls(sitemap):
    def locs(u):
        xml = urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"}), timeout=30).read().decode()
        return re.findall(r"<loc>\s*(.*?)\s*</loc>", xml)
    urls = []
    for s in locs(sitemap):
        urls += locs(s) if s.endswith(".xml") else [s]
    return urls


def close_to_page_one(url, key, domain):
    """URLs Search Console marks as close to page one, so the output can flag links to them. Empty if not set up."""
    req = urllib.request.Request(f"{url}/search/opportunities/{domain}?limit=500", headers={"X-API-Key": key})
    try:
        return {p["url"].rstrip("/").lower() for p in json.load(urllib.request.urlopen(req, timeout=120))["pages"]}
    except Exception:
        return set()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--domain", required=True)
    ap.add_argument("--sitemap", required=True)
    ap.add_argument("--post")
    ap.add_argument("--title", default="")
    ap.add_argument("--providers", default="anthropic,gemini")
    ap.add_argument("--efforts", default="low,medium")
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--url", default=URL, help="the API address (or set POST_DESK_URL)")
    ap.add_argument("--project", default=PROJECT, help="Google Cloud project with the API key secret (or set GOOGLE_CLOUD_PROJECT)")
    a = ap.parse_args()
    if not a.url or not a.project:
        sys.exit("Give --url and --project (or set POST_DESK_URL and GOOGLE_CLOUD_PROJECT).")

    key = subprocess.run(["gcloud", "secrets", "versions", "access", "latest", "--secret", "post-desk-api-key", "--project", a.project],
                         capture_output=True, text=True).stdout.strip()
    if not key:
        sys.exit("Couldn't read the API key from Secret Manager. Are you signed in to gcloud?")
    post = open(a.post).read() if a.post else SAMPLE
    title = a.title or post.strip().splitlines()[0][:120]
    urls = sitemap_urls(a.sitemap)
    print(f"{len(urls)} sitemap URLs, post of {len(post.split())} words\n")
    close = close_to_page_one(a.url, key, a.domain)
    if close:
        print(f"Search Console: {len(close)} pages close to page one\n")

    rows = []
    for provider in a.providers.split(","):
        for effort in a.efforts.split(","):
            for run in range(a.runs):
                body = {"client": {"name": a.domain, "domain": a.domain}, "post": {"title": title, "content": post},
                        "sitemap_urls": urls, "options": {"provider": provider.strip(), "effort": effort.strip()}}
                req = urllib.request.Request(a.url + "/generate", data=json.dumps(body).encode(),
                                             headers={"X-API-Key": key, "Content-Type": "application/json"})
                t0 = time.monotonic()
                try:
                    out = json.load(urllib.request.urlopen(req, timeout=300))
                except urllib.error.HTTPError as e:
                    print(f"{provider}/{effort}: HTTP {e.code} {e.read().decode()[:300]}\n")
                    continue
                s = out["stats"]
                tokens = sum(x.get("output_tokens") or 0 for x in s["model_attempts"])
                rows.append({"provider": provider, "effort": effort, "model": s["model"], "secs": s["ms"] / 1000,
                             "model_secs": s["timings"]["model_ms"] / 1000, "tokens": tokens, "verified": s["verified"],
                             "suggested": s["suggested"], "retries": len(s["model_attempts"]) - 1 - (1 if s["retried"] else 0),
                             "links": [(l["anchor_text"], l["target_url"].split("://", 1)[-1].split("/", 1)[-1]) for l in out["internal_links"]],
                             "meta": out["meta_descriptions"][0]["text"] if out["meta_descriptions"] else "",
                             "metas_ok": sum(m["status"] == "good" for m in out["meta_descriptions"])})
                r = rows[-1]
                r["attempts"] = ", ".join(f"{x['outcome']} {x['ms'] / 1000:.0f}s" for x in s["model_attempts"])
                print(f"=== {provider} / {effort} (run {run + 1}) - {r['model']}")
                print(f"    Model attempts: {r['attempts']}")
                print(f"    {r['model_secs']:.0f}s in the model, {r['tokens']} output tokens, {r['suggested']} links suggested, {r['verified']} verified, "
                      f"{r['metas_ok']} of 3 metas in range")
                sc = s.get("search_console")
                if sc:
                    if sc.get("error"):
                        print(f"    Search Console: not used ({sc['error']})")
                    else:
                        print(f"    Search Console: {sc.get('close_to_page_one_linked', 0)} of {sc.get('close_to_page_one_offered', 0)} "
                              f"close-to-page-one pages offered got a link (property {sc.get('property')})")
                if s.get("main_link_followup"):
                    print(f"    Main service follow-up: {s['main_link_followup']}")
                mix = {k: v for k, v in (s.get("link_mix") or {}).items() if v and k != "close_to_page_one"}
                if mix:
                    print("    Links by page type: " + ", ".join(f"{v} {k.replace('_', ' ')}" for k, v in mix.items()))
                for (anchor, target), link in zip(r["links"], out["internal_links"]):
                    flag = "  [close to page one]" if link["target_url"].rstrip("/").lower() in close else ""
                    print(f'      "{anchor}" -> /{target}{flag}')
                for x in s.get("set_aside") or []:
                    t = x["target_url"].split("://", 1)[-1].split("/", 1)[-1]
                    print(f'      not kept: "{x["anchor_text"]}" -> /{t} ({x["type"]}; {x["why"]})')
                print(f"    Meta: {r['meta']}\n")

    if not rows:
        return
    print("Summary (median across runs)")
    print(f"{'provider':<14}{'effort':<8}{'model time':>11}{'tokens':>8}{'verified':>10}{'metas ok':>10}")
    for provider in a.providers.split(","):
        for effort in a.efforts.split(","):
            g = [r for r in rows if r["provider"] == provider and r["effort"] == effort]
            if g:
                print(f"{provider:<14}{effort:<8}{statistics.median(r['model_secs'] for r in g):>10.0f}s"
                      f"{statistics.median(r['tokens'] for r in g):>8.0f}{statistics.median(r['verified'] for r in g):>10.0f}"
                      f"{statistics.median(r['metas_ok'] for r in g):>10.0f}")


if __name__ == "__main__":
    main()
