# Deploying the API to Cloud Run

One-time setup, about 15 minutes. After this, every push to `main` that changes `service/` runs the tests, builds the image, deploys it and smoke tests the live URL. You can also run **Deploy API to Cloud Run** by hand from the Actions tab.

No Google or Anthropic keys are stored in GitHub. Actions signs in to Google Cloud with Workload Identity Federation (a short-lived token tied to this repo), and the two API keys live in Secret Manager.

## What you need

- A Google Cloud project with billing switched on. Cloud Run has a free tier, and an idle service with `min-instances 0` costs nothing.
- The `gcloud` CLI, signed in (`gcloud auth login`), or Cloud Shell in the browser.
- An Anthropic API key from console.anthropic.com.

## 1. Set some names

```bash
PROJECT=your-project-id
REGION=europe-west2            # London
REPO=cloudnxne/post-desk
gcloud config set project $PROJECT
PROJECT_NUMBER=$(gcloud projects describe $PROJECT --format='value(projectNumber)')
```

## 2. Switch on the services

```bash
gcloud services enable run.googleapis.com artifactregistry.googleapis.com \
  secretmanager.googleapis.com iamcredentials.googleapis.com
```

## 3. Somewhere to keep the images

```bash
gcloud artifacts repositories create post-desk --repository-format=docker --location=$REGION
```

## 4. The two secrets

```bash
# Your Anthropic key (paste it when prompted, then press Ctrl+D)
gcloud secrets create anthropic-api-key --data-file=-

# A random key that callers send in the X-API-Key header
openssl rand -hex 24 | tr -d '\n' | gcloud secrets create post-desk-api-key --data-file=-
```

## 5. Service accounts

One for the running service (can read the secrets, nothing else) and one for GitHub to deploy with.

```bash
gcloud iam service-accounts create post-desk-runtime --display-name="Post Desk API (runtime)"
gcloud iam service-accounts create post-desk-deployer --display-name="Post Desk deploys from GitHub"

RUNTIME=post-desk-runtime@$PROJECT.iam.gserviceaccount.com
DEPLOYER=post-desk-deployer@$PROJECT.iam.gserviceaccount.com

for s in anthropic-api-key post-desk-api-key; do
  gcloud secrets add-iam-policy-binding $s --member=serviceAccount:$RUNTIME --role=roles/secretmanager.secretAccessor
done
# The deployer reads the API key once, for the smoke test after each deploy
gcloud secrets add-iam-policy-binding post-desk-api-key --member=serviceAccount:$DEPLOYER --role=roles/secretmanager.secretAccessor

for r in roles/run.admin roles/artifactregistry.writer; do
  gcloud projects add-iam-policy-binding $PROJECT --member=serviceAccount:$DEPLOYER --role=$r
done
gcloud iam service-accounts add-iam-policy-binding $RUNTIME --member=serviceAccount:$DEPLOYER --role=roles/iam.serviceAccountUser
```

## 6. Let GitHub Actions sign in (Workload Identity Federation)

Only workflows from this one repo can use it.

```bash
gcloud iam workload-identity-pools create github --location=global --display-name="GitHub Actions"
gcloud iam workload-identity-pools providers create-oidc post-desk \
  --location=global --workload-identity-pool=github \
  --issuer-uri=https://token.actions.githubusercontent.com \
  --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository" \
  --attribute-condition="assertion.repository=='$REPO'"

gcloud iam service-accounts add-iam-policy-binding $DEPLOYER \
  --role=roles/iam.workloadIdentityUser \
  --member="principalSet://iam.googleapis.com/projects/$PROJECT_NUMBER/locations/global/workloadIdentityPools/github/attribute.repository/$REPO"

echo "GCP_WIF_PROVIDER = projects/$PROJECT_NUMBER/locations/global/workloadIdentityPools/github/providers/post-desk"
echo "GCP_DEPLOY_SA    = $DEPLOYER"
```

## 7. Tell the repo

On GitHub: **Settings > Secrets and variables > Actions > Variables**, add:

| Variable | Value |
| --- | --- |
| `GCP_PROJECT_ID` | your project ID |
| `GCP_REGION` | `europe-west2` (optional, that's the default) |
| `GCP_WIF_PROVIDER` | printed at the end of step 6 |
| `GCP_DEPLOY_SA` | printed at the end of step 6 |
| `POST_DESK_MODEL` | optional, defaults to `claude-sonnet-5-5` |
| `POST_DESK_EFFORT` | optional, `low` (default), `medium` or `high`: how much the model thinks before answering. A request can override it with `"options": {"effort": "medium"}` |
| `POST_DESK_ALLOWED_DOMAIN` | your Google Workspace domain, e.g. `example.co.uk` (required: the deploy stops without it) |
| `POST_DESK_GOOGLE_AUDIENCES` | optional, see "Who can use it" below |

These are variables, not secrets: none of them gives access on its own.

## 8. Deploy

Actions tab > **Deploy API to Cloud Run** > **Run workflow**. When it's green, the summary shows the service URL.

## Who can use it

Two ways in, and nothing else gets through:

1. **Anyone signed in with a Google account in your Workspace domain.** The request carries their Google sign-in token in an `Authorization: Bearer` header. The API checks Google's signature, that it hasn't expired, that it was issued to an app we trust, and that it's a verified your organisation Workspace account (a personal Google account registered with a work address is turned away). Each run is logged against the person's email.
2. **The shared key** in the `X-API-Key` header, for automations such as n8n.

`POST_DESK_GOOGLE_AUDIENCES` is the list of apps whose sign-ins are accepted, as OAuth client IDs separated by commas. It defaults to gcloud's own client ID (`32555940559.apps.googleusercontent.com`) so you can test from a terminal. The Google Doc front end's Apps Script client ID gets added here too (see "Google Doc front end" below).

To check the lock from Cloud Shell:

```bash
URL=$(gcloud run services describe post-desk-api --region $REGION --format='value(status.url)')
curl -s -X POST "$URL/generate" -H "Authorization: Bearer $(gcloud auth print-identity-token)" \
  -H 'Content-Type: application/json' -d '{}'
```

Signed in as a personal Gmail account, this should say only accounts in your domain can use it. Signed in with your work account (`gcloud auth login you@example.co.uk`), the same request gets past sign-in and fails on the empty body instead (422), which means you're in.

## Trying it

The interactive docs are at `<service-url>/docs`. To call it from a terminal:

```bash
URL=$(gcloud run services describe post-desk-api --region $REGION --format='value(status.url)')
KEY=$(gcloud secrets versions access latest --secret post-desk-api-key)

curl -s "$URL/generate" -H "X-API-Key: $KEY" -H 'Content-Type: application/json' -d '{
  "client": {"name": "Example Landscapes", "domain": "example-landscapes.co.uk"},
  "post": {"title": "Planning Your Dream Garden", "content": "Our garden design service maps out every bed before a spade goes in. We also handle patio installation."},
  "sitemap_urls": ["https://example-landscapes.co.uk/services/garden-design/", "https://example-landscapes.co.uk/services/patio-installation/"]
}' | python3 -m json.tool
```

## Page index: links chosen by meaning (one-time, about 5 minutes)

Without this, the API picks link candidates from words in the URLs, like the page does. With it, the API reads every page on a client's site once, stores an embedding of each (from Vertex AI), and for each new post offers Claude the pages closest in meaning, with their titles and summaries.

Run in Cloud Shell, after step 1's variables are set:

```bash
gcloud services enable aiplatform.googleapis.com storage.googleapis.com

BUCKET=$PROJECT-post-desk-index
gcloud storage buckets create gs://$BUCKET --location=$REGION --uniform-bucket-level-access

RUNTIME=post-desk-runtime@$PROJECT.iam.gserviceaccount.com
gcloud projects add-iam-policy-binding $PROJECT --member=serviceAccount:$RUNTIME --role=roles/aiplatform.user
gcloud storage buckets add-iam-policy-binding gs://$BUCKET --member=serviceAccount:$RUNTIME --role=roles/storage.objectAdmin
echo "POST_DESK_BUCKET = $BUCKET"
```

Check embeddings work in your project (costs a fraction of a penny):

```bash
curl -sO https://raw.githubusercontent.com/cloudnxne/post-desk/main/service/scripts/check_vertex.py
pip install --quiet --user google-genai numpy
python3 check_vertex.py --project $PROJECT
```

It should end with "Best match: Composite decking". If it says the model isn't available in europe-west2, run it again with `--location us-central1` and set `POST_DESK_VERTEX_LOCATION` to match.

Then add the repo variable `POST_DESK_BUCKET` (printed above) and run the deploy workflow. Optional variables: `POST_DESK_VERTEX_LOCATION` (default `europe-west2`), `POST_DESK_EMBED_MODEL` (default `gemini-embedding-001`).

### Indexing a client

Give the client's domain and sitemap. Each call works for up to about 3 minutes and says how many pages are `remaining`; call again until it's 0. After that, re-running only re-reads pages older than 30 days and only re-embeds pages whose text changed, so it's cheap to run on a schedule.

```bash
URL=$(gcloud run services describe post-desk-api --region $REGION --format='value(status.url)')
KEY=$(gcloud secrets versions access latest --secret post-desk-api-key)

curl -s -X POST "$URL/index" -H "X-API-Key: $KEY" -H 'Content-Type: application/json' -d '{
  "client": {"name": "Client Name", "domain": "client-site.co.uk"},
  "sitemap_url": "https://www.client-site.co.uk/sitemap.xml"
}' | python3 -m json.tool

curl -s "$URL/index/client-site.co.uk" -H "X-API-Key: $KEY" | python3 -m json.tool
```

From then on, `/generate` for that client (same domain) uses the index automatically. Its stats show `"retrieval": "semantic"`; without an index they show `"url"`.

The crawler is deliberately gentle: 2 pages at a time, half a second apart, and slower if the site's robots.txt asks for a Crawl-delay. If a site's firewall starts refusing (5 timeouts or "too many requests" in a row), it pauses that run and says so in `stopped_early`; pages that failed for a temporary reason are listed under `retry_next_run` and tried again next time, so just run it again later. `skip_examples` shows a few URLs for each reason. To change the pace, set `POST_DESK_CRAWL_CONCURRENCY` and `POST_DESK_CRAWL_DELAY_S` on the service.

What the crawler won't do: fetch anything outside the client's own domain, follow redirects elsewhere, reach private or internal addresses, ignore robots.txt, or index pages marked noindex. Pages marked noindex or returning 404 are never offered as link targets.

## Choosing the model: Claude or Gemini

The API can use three models, set for the whole service with the repo variable `POST_DESK_PROVIDER`, or for a single request with `"options": {"provider": "..."}`:

| Provider | Model | Signs in with | Bill |
| --- | --- | --- | --- |
| `anthropic` (default) | Claude, `POST_DESK_MODEL` | the Anthropic key in Secret Manager | Anthropic |
| `gemini` | Gemini, `POST_DESK_GEMINI_MODEL` (default `gemini-3.8-flash`) | the Cloud Run service account, no key | Google Cloud |
| `claude-vertex` | Claude, `POST_DESK_CLAUDE_VERTEX_MODEL` | the Cloud Run service account, no key | Google Cloud |

Gemini needs nothing extra: the page index setup already switched on Vertex AI and gave the service account the Vertex AI User role. `POST_DESK_GEMINI_LOCATION` defaults to `global`.

Claude on Vertex needs the model switching on once: in the Google Cloud console, open **Vertex AI > Model Garden**, find the Claude model, and click **Enable** (it asks you to accept Anthropic's terms). Use the exact model ID shown there for `POST_DESK_CLAUDE_VERTEX_MODEL`.

`effort` (`low`, `medium`, `high`) works for every provider; for Gemini it sets the thinking level.

### Automatic fallback

By default Claude answers, and if Claude fails for any reason (down, too slow, rate limited, or a reply that isn't valid JSON), Gemini answers instead, so a post still gets its metas and links. The run's stats say which model answered (`provider`, `model`) and, if it fell back, why (`fell_back`). Timings stay inside Cloud Run's limit: Claude gets up to two 45 second tries, Gemini one 60 second try, and once a run has fallen back its anchor fix-up goes straight to Gemini.

- Change the fallback with the repo variable `POST_DESK_FALLBACK_PROVIDER` (default `gemini`), or set it to `none` to switch it off.
- Naming a model in a request (`"options": {"provider": "anthropic"}`) turns the fallback off for that request, so comparisons stay fair.
- To check the fallback works, send `"options": {"force_fallback": true}` with the API key: Claude is skipped and the stats should show `"provider": "gemini"` with `fell_back` set.

### Comparing them

Run the same post through each model on the live service and get a side-by-side table (time, tokens, links verified, metas in range, and the links themselves):

```bash
curl -sO https://raw.githubusercontent.com/cloudnxne/post-desk/main/service/scripts/compare_models.py
python3 compare_models.py --domain lawnco.example.co.uk --sitemap https://lawnco.example.co.uk/sitemap_index.xml
```

Add `--runs 3` for fairer timings, `--post my-post.txt` to test a real draft, or `--efforts low,medium,high`. Once you've picked a winner, set `POST_DESK_PROVIDER` (and `POST_DESK_EFFORT`) as repo variables and re-run the deploy workflow. If you switch fully to Gemini or Claude on Vertex, the Anthropic secret is no longer needed.

## Search Console ranking data

With this, Post Desk knows which of a client's pages sit just off Google's first page (average position 8 to 20 over the last 90 days, with at least 50 impressions). Those are the pages a new internal link is most likely to help, so they're brought into the link candidates when relevant, marked "close to page one" for Claude with their position, impressions and top searches, and Claude is told to favour them and use their search wording as anchors. A post's stats show how many were offered and linked. Nobody at your organisation has to do anything per user: it's set up once.

First, switch the API on: `gcloud services enable searchconsole.googleapis.com --project my-project`

There are two ways to give it access. If both are set up, the connected account is used.

### Option A: connect one account (reads every property that account can see)

Best for testing with your own Search Console, or one work account that already has access to the clients.

1. In the Google Cloud console, open **Google Auth Platform** (APIs & Services > OAuth consent screen). Set an app name (Post Desk), choose **External**, and under **Audience** add the Gmail you'll sign in with as a **test user**.
2. Under **Clients**, create a client of type **Desktop app** and download its JSON file.
3. In Cloud Shell, upload that file (the three-dot menu > Upload), then:
   ```bash
   curl -sO https://raw.githubusercontent.com/cloudnxne/post-desk/main/service/scripts/connect_gsc.py
   python3 connect_gsc.py --client-file client_secret_XXXX.json
   ```
   Open the link it prints, sign in, then copy the address of the page that fails to load (localhost) and paste it back. It lists the properties it can read and saves the sign-in to Secret Manager as `gsc-credentials`.
4. On GitHub add the repo variable `POST_DESK_GSC_SECRET` = `gsc-credentials`, then re-run the deploy workflow.

While the app's publishing status is **Testing**, Google expires the sign-in after 7 days, so just run step 3 again (the secret gets a new version). For longer use, click **Publish app** under Audience: you'll see an "unverified app" warning when signing in, which is fine for your own account.

The saved sign-in is read only, but it can read every property that account can see, so for client data use a work account and, ideally, an your organisation-owned Google Cloud project.

### Option B: add the service account to each client's property (reads only those properties)

In Search Console, as an owner of the property: **Settings > Users and permissions > Add user**, enter `post-desk-runtime@my-project.iam.gserviceaccount.com`, permission **Restricted**. Nothing else to configure.

### Checking it

```bash
URL=https://YOUR-POST-DESK-API.run.app
KEY=$(gcloud secrets versions access latest --secret post-desk-api-key --project my-project)
curl -s "$URL/search/properties" -H "X-API-Key: $KEY" | python3 -m json.tool
curl -s "$URL/search/opportunities/your-site.co.uk" -H "X-API-Key: $KEY" | python3 -m json.tool
```

The first lists what it can read; the second lists the site's pages close to page one with their position, impressions and top searches. For the data to shape link picks, the site also needs its page index (see "Indexing a client"). Settings: `POST_DESK_GSC_DAYS` (90), `POST_DESK_GSC_MIN_POS` (8), `POST_DESK_GSC_MAX_POS` (20), `POST_DESK_GSC_MIN_IMPRESSIONS` (50), `POST_DESK_GSC=off` to switch it off.

## Google Doc front end

A **Post Desk** menu in a Google Doc. The sidebar sends the post to the API signed in as whoever is using it, shows the SEO title, three meta descriptions and the internal links, and adds the ticked links to the doc as real hyperlinks. It never sends a sitemap. The client list is the person's own Search Console properties (the doc sends their sign-in for that request only, never stored, with Search Console data cached per person), any site that's been indexed, and any client added by hand with "Client not listed?" (shared with everyone using the script). For a client their own account can't see, Search Console data comes from the shared connected account. For an indexed client the API uses the page index; for one that hasn't been read yet it finds the sitemap itself (robots.txt, then the usual WordPress and Yoast addresses) and picks links from the page addresses, and the sidebar offers "Read the client's site" for better, meaning-based picks.

The code is in `apps-script/` (three files). Set it up once, in a doc owned by your **work** account:

1. Open a Google Doc and go to **Extensions > Apps Script**.
2. In the Apps Script editor, open **Project Settings** (the cog) and tick **Show "appsscript.json" manifest file in editor**.
3. Back in **Editor**:
   - Replace everything in `Code.gs` with `apps-script/Code.gs` from the repo.
   - Replace everything in `appsscript.json` with `apps-script/appsscript.json`.
   - Click **+**, choose **HTML**, name it `Sidebar` (no `.html`), and paste in `apps-script/Sidebar.html`.
   - Save (Ctrl+S).
4. Reload the doc. A **Post Desk** menu appears. Choose **Post Desk > Check sign-in** and allow the permissions it asks for: this doc only, showing a sidebar, calling the API and your email address.
5. The check shows a **Script client ID**, ending `.apps.googleusercontent.com`. On GitHub, add the repo variable `POST_DESK_GOOGLE_AUDIENCES` with gcloud's ID and that one, separated by a comma:
   `32555940559.apps.googleusercontent.com,<script client ID>`
   Then re-run the deploy workflow.
6. Run **Check sign-in** again. It should say Post Desk accepted your sign-in.

Then **Post Desk > Open Post Desk**, choose the client and click **Find links and meta descriptions**. Client notes and priority pages are saved for everyone using the script, and each doc remembers its client.

**Using it in all your docs (no admin needed).** The same script is also a Google Workspace side-panel add-on (`Card.gs`). Add `Card.gs` as a script file next to `Code.gs`, update `appsscript.json`, save, then in the Apps Script editor choose **Deploy > Test deployments**, and next to **Google Workspace add-on** click **Install**. Open any Google Doc and the Post Desk icon appears in the panel on the right; the first time in each doc it asks to be allowed to use that doc. It's the same script, so the client ID already in `POST_DESK_GOOGLE_AUDIENCES` covers it. Google limits these panels to a simpler card layout: the HTML shows in a box to copy, and each button press must finish within 30 seconds, so reading a site runs in 20 second steps.

Rolling it out to the whole team needs it published as an internal Google Workspace add-on, which needs a Google Cloud project owned by your organisation and your Google Workspace admin's approval.

**Time limits.** Apps Script stops a web request after about a minute, so the sidebar reads a new client's site in short steps (45 seconds each, up to five per click) and asks you to click again if there's more. A normal post takes 10 to 20 seconds.

## Logs

Each run writes one JSON line (run ID, client, candidates, links suggested, verified, dropped, retried, model, time taken). In the console: **Cloud Run > post-desk-api > Logs**, or:

```bash
gcloud logging read 'resource.labels.service_name="post-desk-api" AND jsonPayload.event="generate"' --limit 20 --format json
```
