"""Who is calling. Two ways in:

1. A Google sign-in (ID token in "Authorization: Bearer ...") from a verified account in the allowed
   Google Workspace domain, e.g. anyone at example.co.uk. This is how people use it: the Google Doc
   front end sends the signed-in user's token automatically.
2. The shared key in "X-API-Key", for automations such as n8n.

Settings (environment variables, read on each request):
  POST_DESK_ALLOWED_DOMAIN    Workspace domain allowed to sign in, e.g. example.co.uk
  POST_DESK_GOOGLE_AUDIENCES  comma-separated OAuth client IDs the token may be issued to
                              (the Apps Script project's client ID, and optionally gcloud's for testing)
  POST_DESK_API_KEY           the shared key; leave unset to allow Google sign-in only
"""
from __future__ import annotations

import hmac
import os
import threading
import time
from dataclasses import dataclass

from fastapi import Depends, HTTPException
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer

# The client ID gcloud uses, so `gcloud auth print-identity-token` works for testing if added to the audiences.
GCLOUD_CLIENT_ID = "32555940559.apps.googleusercontent.com"

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False, description="Shared key for automations")
bearer = HTTPBearer(auto_error=False, description="Google ID token from an account in the allowed domain")


class SignInUnavailable(Exception):
    """Google's signing certificates couldn't be fetched, so no sign-in can be checked right now."""


@dataclass(frozen=True)
class Caller:
    kind: str            # "google" or "api_key"
    email: str | None = None


def _csv(name: str) -> list[str]:
    return [v.strip() for v in os.environ.get(name, "").split(",") if v.strip()]


class _CachedRequest:
    """google-auth transport that caches Google's public signing certs for an hour,
    so each request doesn't fetch them again."""

    TTL = 3600

    def __init__(self):
        from google.auth.transport import requests as g_requests

        self._inner = g_requests.Request()
        self._cache: dict[str, tuple[float, object]] = {}
        self._lock = threading.Lock()

    def __call__(self, url, method="GET", **kwargs):
        if method != "GET":
            return self._inner(url, method=method, **kwargs)
        with self._lock:
            hit = self._cache.get(url)
            if hit and time.monotonic() - hit[0] < self.TTL:
                return hit[1]
        resp = self._inner(url, method=method, **kwargs)
        if resp.status == 200:
            with self._lock:
                self._cache[url] = (time.monotonic(), resp)
        return resp


_request: _CachedRequest | None = None


def verify_google_token(token: str, audiences: list[str]) -> dict:
    """Checks the token's signature, expiry, issuer and audience. Raises ValueError if any fail,
    or SignInUnavailable if Google can't be reached to fetch its certificates."""
    global _request
    from google.auth import exceptions as g_exc
    from google.oauth2 import id_token

    if _request is None:
        _request = _CachedRequest()
    try:
        return dict(id_token.verify_oauth2_token(token, _request, audience=audiences, clock_skew_in_seconds=10))
    except g_exc.TransportError as e:
        raise SignInUnavailable(str(e)) from e
    except g_exc.GoogleAuthError as e:
        raise ValueError(str(e)) from e


def domain_allowed(claims: dict, domain: str) -> bool:
    """A verified address in the domain AND a Workspace account for it (the hd claim).
    The hd check stops a personal Google account registered with a work address getting in."""
    email = str(claims.get("email") or "").lower()
    return (claims.get("email_verified") is True
            and email.endswith("@" + domain)
            and str(claims.get("hd") or "").lower() == domain)


def require_caller(key: str | None = Depends(api_key_header),
                   cred: HTTPAuthorizationCredentials | None = Depends(bearer)) -> Caller:
    domain = os.environ.get("POST_DESK_ALLOWED_DOMAIN", "").strip().lower()
    audiences = _csv("POST_DESK_GOOGLE_AUDIENCES")
    expected_key = os.environ.get("POST_DESK_API_KEY", "").strip()
    google_on = bool(domain and audiences)

    if not google_on and not expected_key:
        raise HTTPException(503, "The service has no sign-in set up, so it won't accept requests.")

    if cred and cred.scheme.lower() == "bearer":
        if not google_on:
            raise HTTPException(401, "Google sign-in isn't switched on for this service.")
        try:
            claims = verify_google_token(cred.credentials, audiences)
        except SignInUnavailable:
            raise HTTPException(503, "Couldn't reach Google to check your sign-in. Please try again in a moment.")
        except ValueError:
            raise HTTPException(401, "That Google sign-in isn't valid or has expired.", headers={"WWW-Authenticate": "Bearer"})
        if not domain_allowed(claims, domain):
            raise HTTPException(403, f"Only {domain} Google accounts can use Post Desk.")
        return Caller("google", str(claims["email"]).lower())

    if key and expected_key and hmac.compare_digest(key, expected_key):
        return Caller("api_key")

    raise HTTPException(401, f"Sign in with your {domain} Google account, or send the X-API-Key header."
                        if google_on else "Missing or wrong X-API-Key header.")
