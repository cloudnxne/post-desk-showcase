"""Connects a Google account's Search Console to Post Desk, read only. Run once in Cloud Shell.

    curl -sO https://raw.githubusercontent.com/cloudnxne/post-desk/main/service/scripts/connect_gsc.py
    python3 connect_gsc.py --client-file client_secret.json

What it does:
  1. Prints a Google sign-in link. Open it in your own browser and sign in with the account that has the
     Search Console access (a personal one to test, a work one for real use).
  2. Google then sends your browser to a localhost page that won't load. That's expected: copy the whole
     address from the address bar and paste it back here.
  3. It checks the sign-in by listing the Search Console properties the account can see.
  4. It saves the sign-in to Secret Manager as "gsc-credentials" and lets the Post Desk service read it.

It only asks for read-only Search Console access (plus your email address, to label which account is connected).
Nothing is stored on this machine. To disconnect, delete the secret or remove access at myaccount.google.com/permissions.
"""
import argparse
import os
import base64
import hashlib
import json
import secrets
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

SCOPES = ["https://www.googleapis.com/auth/webmasters.readonly", "openid", "email"]
REDIRECT = "http://localhost:8765/"
def default_project() -> str:
    """The project gcloud is set to (Cloud Shell sets this for you), or GOOGLE_CLOUD_PROJECT."""
    p = os.environ.get("GOOGLE_CLOUD_PROJECT", "")
    if not p:
        p = subprocess.run(["gcloud", "config", "get-value", "project"], capture_output=True, text=True).stdout.strip()
    return p


def post(url, data):
    req = urllib.request.Request(url, data=urllib.parse.urlencode(data).encode(), method="POST")
    try:
        return json.load(urllib.request.urlopen(req, timeout=30))
    except urllib.error.HTTPError as e:
        sys.exit(f"Google said {e.code}: {e.read().decode()[:300]}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--client-file", required=True, help="the OAuth client JSON downloaded from the console (Desktop app)")
    ap.add_argument("--secret", default="gsc-credentials")
    ap.add_argument("--project", default=None, help="Google Cloud project (default: gcloud's current project)")
    a = ap.parse_args()
    a.project = a.project or default_project()
    if not a.project:
        sys.exit("Couldn't tell which Google Cloud project to use. Pass --project.")

    raw = json.load(open(a.client_file))
    c = raw.get("installed") or raw.get("web") or raw
    client_id, client_secret = c.get("client_id"), c.get("client_secret")
    if not client_id or not client_secret:
        sys.exit("That file doesn't look like an OAuth client JSON (no client_id / client_secret).")

    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode({
        "client_id": client_id, "redirect_uri": REDIRECT, "response_type": "code", "scope": " ".join(SCOPES),
        "access_type": "offline", "prompt": "consent", "state": state,
        "code_challenge": challenge, "code_challenge_method": "S256"})
    print("\n1. Open this link in your browser and sign in with the account that has Search Console access:\n")
    print(url)
    print("\n2. Your browser will end up on a localhost page that doesn't load. Copy the full address from the address bar.")
    back = input("\nPaste it here: ").strip()

    q = urllib.parse.parse_qs(urllib.parse.urlsplit(back).query)
    if q.get("error"):
        sys.exit(f"Google said: {q['error'][0]}")
    if q.get("state", [""])[0] != state:
        sys.exit("That address doesn't match this sign-in (state mismatch). Run the script again.")
    code = q.get("code", [""])[0]
    if not code:
        sys.exit("No code in that address. Make sure you copied the whole thing.")

    tok = post("https://oauth2.googleapis.com/token", {"code": code, "client_id": client_id, "client_secret": client_secret,
                                                        "redirect_uri": REDIRECT, "grant_type": "authorization_code",
                                                        "code_verifier": verifier})
    if not tok.get("refresh_token"):
        sys.exit("Google didn't return a refresh token. Remove Post Desk at myaccount.google.com/permissions and run this again.")
    account = ""
    if tok.get("id_token"):
        payload = tok["id_token"].split(".")[1]
        account = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))).get("email", "")

    req = urllib.request.Request("https://www.googleapis.com/webmasters/v3/sites",
                                 headers={"Authorization": f"Bearer {tok['access_token']}"})
    try:
        sites = json.load(urllib.request.urlopen(req, timeout=30)).get("siteEntry", [])
    except urllib.error.HTTPError as e:
        sys.exit(f"Signed in, but Search Console said {e.code}: {e.read().decode()[:300]}\n"
                 f"Check the Search Console API is enabled: gcloud services enable searchconsole.googleapis.com")
    print(f"\n3. Signed in as {account or '(email not shared)'}. Search Console properties it can read:")
    for s in sites:
        print(f"   {s.get('siteUrl')}  ({s.get('permissionLevel')})")
    if not sites:
        print("   (none: this account has no Search Console properties)")

    saved = json.dumps({"type": "authorized_user", "client_id": client_id, "client_secret": client_secret,
                        "refresh_token": tok["refresh_token"], "account": account})
    exists = subprocess.run(["gcloud", "secrets", "describe", a.secret, "--project", a.project], capture_output=True).returncode == 0
    cmd = (["gcloud", "secrets", "versions", "add", a.secret] if exists
           else ["gcloud", "secrets", "create", a.secret, "--replication-policy", "automatic"])
    r = subprocess.run(cmd + ["--project", a.project, "--data-file=-"], input=saved, text=True, capture_output=True)
    if r.returncode:
        sys.exit(f"Couldn't save to Secret Manager: {r.stderr[:300]}")
    subprocess.run(["gcloud", "secrets", "add-iam-policy-binding", a.secret, "--project", a.project, "--quiet",
                    f"--member=serviceAccount:post-desk-runtime@{a.project}.iam.gserviceaccount.com", "--role=roles/secretmanager.secretAccessor"],
                   capture_output=True, text=True)
    print(f"\n4. Saved as the secret \"{a.secret}\" and gave the Post Desk service read access to it.")
    print(f"   Next: on GitHub add the repo variable POST_DESK_GSC_SECRET = {a.secret}, then re-run the deploy workflow.")


if __name__ == "__main__":
    main()
