#!/usr/bin/env sh
# Smoke test against a running Post Desk API (local container or Cloud Run).
# Usage: ./smoke_test.sh http://localhost:8080 <api-key>
# Checks health, auth and validation without calling the model, so it costs nothing to run.
set -eu
BASE="${1:?base URL}"
KEY="${2:?API key}"

code() { curl -s -o /dev/null -w '%{http_code}' "$@"; }
check() {
  if [ "$2" = "$3" ]; then echo "ok   $1 ($2)"; return; fi
  echo "FAIL $1: expected $3, got $2"
  # On GitHub Actions, also raise an annotation so the failure shows on the run summary.
  [ -n "${GITHUB_ACTIONS:-}" ] && echo "::error title=Smoke test::$1 expected $3, got $2"
  exit 1
}

check "health"            "$(code "$BASE/health")" 200
check "API docs"          "$(code "$BASE/docs")" 200
check "no key"            "$(code -X POST "$BASE/generate" -H 'Content-Type: application/json' -d '{}')" 401
check "bad sign-in"       "$(code -X POST "$BASE/generate" -H 'Authorization: Bearer not-a-token' -H 'Content-Type: application/json' -d '{}')" 401
check "wrong key"         "$(code -X POST "$BASE/generate" -H 'X-API-Key: wrong' -H 'Content-Type: application/json' -d '{}')" 401
check "index needs sign-in" "$(code "$BASE/index/example.com")" 401
check "search needs sign-in" "$(code "$BASE/search/properties")" 401
check "invalid request"   "$(code -X POST "$BASE/generate" -H "X-API-Key: $KEY" -H 'Content-Type: application/json' -d '{"client":{"name":"x"},"post":{"content":""},"sitemap_urls":[]}')" 422
echo "Smoke test passed for $BASE"
