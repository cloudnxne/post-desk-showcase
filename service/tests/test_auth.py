"""Sign-in rules. Tokens here are real signed JWTs, made with a test key that stands in for Google's,
so signature, expiry, issuer and audience checks all run for real."""
import datetime as dt
import time

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from google.auth import crypt, jwt

from app import auth
from conftest import API_KEY, FIRST_ANSWER, RETRY_ANSWER, body

AUD = "123-appsscript.apps.googleusercontent.com"
DOMAIN = "agency.example.co.uk"


def _key_and_cert(cn):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(1).not_valid_before(now - dt.timedelta(days=1)).not_valid_after(now + dt.timedelta(days=1))
            .sign(key, hashes.SHA256()))
    pem_key = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    return pem_key, cert.public_bytes(serialization.Encoding.PEM).decode()


GOOGLE_KEY, GOOGLE_CERT = _key_and_cert("google-test")
FORGER_KEY, _ = _key_and_cert("forger")


def token(signing_key=GOOGLE_KEY, kid="google-kid", **over):
    now = int(time.time())
    claims = {"iss": "https://accounts.google.com", "aud": AUD, "iat": now, "exp": now + 3600,
              "sub": "1", "email": "sam.jones@agency.example.co.uk", "email_verified": True, "hd": DOMAIN}
    claims.update(over)
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(crypt.RSASigner.from_string(signing_key, kid), claims).decode()


@pytest.fixture
def signed_in(api, monkeypatch):
    """The API with Google sign-in on for agency.example.co.uk, Google's certs swapped for the test cert."""
    monkeypatch.setenv("POST_DESK_ALLOWED_DOMAIN", DOMAIN)
    monkeypatch.setenv("POST_DESK_GOOGLE_AUDIENCES", f"{AUD}, {auth.GCLOUD_CLIENT_ID}")
    monkeypatch.setattr("google.oauth2.id_token._fetch_certs", lambda request, url: {"google-kid": GOOGLE_CERT})
    monkeypatch.setattr(auth, "_request", object())
    client, set_model = api
    set_model(FIRST_ANSWER, RETRY_ANSWER)
    return client


def call(client, tok=None, key=None):
    headers = {"X-API-Key": key or ""}
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    return client.post("/generate", json=body(), headers=headers)


# ---------- who gets in ----------
def test_agency_account_gets_in(signed_in, caplog):
    r = call(signed_in, token())
    assert r.status_code == 200, r.text
    assert '"user": "sam.jones@agency.example.co.uk"' in caplog.text   # each run is logged against the person


def test_email_case_doesnt_matter(signed_in):
    assert call(signed_in, token(email="Sam.Jones@Agency.example.co.uk", hd="Agency.example.co.uk")).status_code == 200


def test_gcloud_tokens_work_for_testing_when_allowed(signed_in):
    assert call(signed_in, token(aud=auth.GCLOUD_CLIENT_ID)).status_code == 200


def test_api_key_still_works_for_automations(signed_in):
    assert call(signed_in, key=API_KEY).status_code == 200


# ---------- who doesn't ----------
def test_other_domain_is_refused(signed_in):
    r = call(signed_in, token(email="someone@gmail.com", hd=None))
    assert r.status_code == 403 and "agency.example.co.uk" in r.json()["detail"]


def test_lookalike_domain_is_refused(signed_in):
    assert call(signed_in, token(email="x@notagency.example.co.uk", hd="notagency.example.co.uk")).status_code == 403


def test_personal_google_account_with_a_work_address_is_refused(signed_in):
    # A consumer Google account can be registered with a work email; it has no hd claim.
    assert call(signed_in, token(hd=None)).status_code == 403


def test_unverified_email_is_refused(signed_in):
    assert call(signed_in, token(email_verified=False)).status_code == 403


def test_token_for_another_app_is_refused(signed_in):
    assert call(signed_in, token(aud="someone-elses-app.apps.googleusercontent.com")).status_code == 401


def test_expired_token_is_refused(signed_in):
    old = int(time.time()) - 7200
    assert call(signed_in, token(iat=old, exp=old + 3600)).status_code == 401


def test_wrong_issuer_is_refused(signed_in):
    assert call(signed_in, token(iss="https://evil.example.com")).status_code == 401


def test_forged_signature_is_refused(signed_in):
    assert call(signed_in, token(signing_key=FORGER_KEY)).status_code == 401


def test_garbage_token_is_refused(signed_in):
    assert call(signed_in, "not-a-token").status_code == 401


def test_bad_token_doesnt_fall_back_to_a_good_key(signed_in):
    # If someone sends a sign-in, it's the sign-in that's judged.
    assert call(signed_in, "not-a-token", key=API_KEY).status_code == 401


def test_no_credentials_at_all(signed_in):
    r = call(signed_in)
    assert r.status_code == 401 and "Google account" in r.json()["detail"]


def test_google_unreachable_is_a_retry_not_a_rejection(signed_in, monkeypatch):
    from google.auth.exceptions import TransportError

    def down(request, url):
        raise TransportError("connection refused")
    monkeypatch.setattr("google.oauth2.id_token._fetch_certs", down)
    r = call(signed_in, token())
    assert r.status_code == 503 and "try again" in r.json()["detail"]


# ---------- configuration ----------
def test_google_only_mode_when_no_api_key_is_set(signed_in, monkeypatch):
    monkeypatch.delenv("POST_DESK_API_KEY")
    assert call(signed_in, token()).status_code == 200
    assert call(signed_in, key="anything").status_code == 401


def test_api_key_with_a_stray_line_break_still_matches(signed_in, monkeypatch):
    monkeypatch.setenv("POST_DESK_API_KEY", API_KEY + "\n")
    assert call(signed_in, key=API_KEY).status_code == 200


def test_bearer_refused_when_google_sign_in_is_off(api, monkeypatch):
    client, set_model = api
    set_model(FIRST_ANSWER)
    monkeypatch.delenv("POST_DESK_ALLOWED_DOMAIN", raising=False)
    assert call(client, token()).status_code == 401


def test_nothing_configured_refuses_everything(api, monkeypatch):
    client, set_model = api
    set_model(FIRST_ANSWER)
    monkeypatch.delenv("POST_DESK_API_KEY")
    assert call(client, token()).status_code == 503


@pytest.mark.parametrize("claims, ok", [
    ({"email": "a@agency.example.co.uk", "email_verified": True, "hd": "agency.example.co.uk"}, True),
    ({"email": "a@agency.example.co.uk", "email_verified": "true", "hd": "agency.example.co.uk"}, False),   # must be a real true
    ({"email": "a@agency.example.co.uk.evil.com", "email_verified": True, "hd": "agency.example.co.uk"}, False),
    ({"email": "a@sub.agency.example.co.uk", "email_verified": True, "hd": "sub.agency.example.co.uk"}, False),
    ({"email_verified": True, "hd": "agency.example.co.uk"}, False),
])
def test_domain_rule(claims, ok):
    assert auth.domain_allowed(claims, DOMAIN) is ok


def test_docs_page_offers_both_sign_in_options(api):
    client, _ = api
    schemes = client.get("/openapi.json").json()["components"]["securitySchemes"]
    assert {s["type"] for s in schemes.values()} == {"apiKey", "http"}
