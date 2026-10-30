"""Model clients. The service only needs "send a prompt, get JSON back", so each provider sits behind one small interface:
json_with_info(prompt, attempts=, timeout=, effort=) -> (answer, attempts log).

Providers (POST_DESK_PROVIDER, or per request with options.provider). POST_DESK_FALLBACK_PROVIDER (default gemini;
"none" to switch off) answers when the main one fails, unless the request named a provider itself:
  anthropic     Claude through the Anthropic API (needs ANTHROPIC_API_KEY)
  gemini        Gemini through Vertex AI (signs in as the Cloud Run service account, no key)
  claude-vertex Claude through Vertex AI (no key; the Claude model must be enabled in Model Garden first)
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Protocol


class ModelError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class JSONModel(Protocol):
    name: str

    def json(self, prompt: str) -> dict: ...


def parse_json(text: str) -> dict:
    """Pulls the JSON object out of a model reply, allowing for code fences or a sentence either side."""
    t = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", text.strip())
    try:
        out = json.loads(t)
    except json.JSONDecodeError:
        start, end = t.find("{"), t.rfind("}")
        if start < 0 or end <= start:
            raise ModelError("invalid_json", "The model's answer had no JSON in it.")
        try:
            out = json.loads(t[start: end + 1])
        except json.JSONDecodeError as e:
            raise ModelError("invalid_json", f"The model's answer was malformed JSON: {e.msg}") from e
    if not isinstance(out, dict):
        raise ModelError("invalid_json", "The model's answer was JSON but not an object.")
    return out


EFFORTS = ("low", "medium", "high")

# Connecting gets its own short limit. Each address a server name resolves to is tried in turn, and with one shared
# limit a single unresponsive address used to cost the whole 70 seconds before the next (working) one was tried:
# every slow run in the logs was exactly our cut-off plus a normal 7 to 26 second reply (116s at 90, 77s and 89s at 70).
CONNECT_TIMEOUT = 5


def connect_capped_httpx_client():
    """An httpx client for Google's SDK that turns its single per-request timeout into
    "connect within CONNECT_TIMEOUT, then wait up to the full timeout for the reply"."""
    import httpx

    class _Client(httpx.Client):
        def build_request(self, *args, **kwargs):
            t = kwargs.get("timeout")
            if isinstance(t, (int, float)):
                kwargs["timeout"] = httpx.Timeout(t, connect=min(float(t), CONNECT_TIMEOUT))
            return super().build_request(*args, **kwargs)

    return _Client(timeout=httpx.Timeout(70, connect=CONNECT_TIMEOUT))


class AnthropicModel:
    """Claude through the Anthropic API. Needs ANTHROPIC_API_KEY; the model is set with POST_DESK_MODEL.

    Retries are done here rather than inside the SDK, with a shorter cut-off per attempt, so a stalled call is
    abandoned sooner and every attempt (how long it took, how it ended) is recorded for the run's stats."""

    ATTEMPT_TIMEOUT = 70     # seconds per attempt
    ATTEMPTS = 2             # main call: one retry. Worst case 140s, well inside Cloud Run's 300s.

    def __init__(self, model: str | None = None, api_key: str | None = None, max_tokens: int = 8000):
        import anthropic  # imported here so tests and other providers don't need it

        self.provider = "anthropic"
        self.name = model or os.environ.get("POST_DESK_MODEL", "claude-sonnet-5-5")
        self.max_tokens = max_tokens
        # How much Claude thinks before answering. The API default is "high". Measured on real posts, low gave links and
        # metas as good as medium and high, with the fewest tokens, so low is the default here.
        self.effort = os.environ.get("POST_DESK_EFFORT", "low").strip() or "low"
        # .strip(): a key pasted into Secret Manager often ends with a line break, which makes it an illegal header.
        key = (api_key or os.environ.get("ANTHROPIC_API_KEY") or "").strip()
        self._client = self._make_client(anthropic, key)
        self._anthropic = anthropic

    def _timeout(self, seconds: float | None = None):
        return self._anthropic_mod.Timeout(seconds or self.ATTEMPT_TIMEOUT, connect=CONNECT_TIMEOUT)

    def _make_client(self, anthropic, key):
        self._anthropic_mod = anthropic
        return anthropic.Anthropic(api_key=key, max_retries=0, timeout=self._timeout())

    def json(self, prompt: str) -> dict:
        return self.json_with_info(prompt)[0]

    def json_with_info(self, prompt: str, attempts: int | None = None, timeout: float | None = None,
                       effort: str | None = None) -> tuple[dict, list[dict]]:
        """The parsed answer, plus one record per attempt: {"ms": ..., "outcome": "ok" | "timeout" | ..., "effort": ...}."""
        a = self._anthropic
        effort = effort or self.effort
        tries = attempts or self.ATTEMPTS
        log: list[dict] = []
        for n in range(tries):
            t0 = time.monotonic()
            try:
                msg = self._client.messages.create(model=self.name, max_tokens=self.max_tokens, timeout=self._timeout(timeout),
                                                   output_config={"effort": effort},
                                                   messages=[{"role": "user", "content": prompt}])
            except (a.APITimeoutError, a.APIConnectionError, a.InternalServerError, a.RateLimitError) as e:
                outcome = {a.APITimeoutError: "timeout", a.RateLimitError: "rate_limited"}.get(type(e), "server_error")
                if isinstance(e, a.APIConnectionError) and not isinstance(e, a.APITimeoutError):
                    outcome = "connection_error"
                log.append({"ms": int((time.monotonic() - t0) * 1000), "outcome": outcome, "effort": effort, "provider": self.provider})
                if n + 1 < tries:
                    time.sleep(2 if outcome == "rate_limited" else 0.5)
                    continue
                if outcome == "timeout":
                    raise ModelError("model_timeout", "The model took too long to answer. Please try again.") from e
                if outcome == "rate_limited":
                    raise ModelError("rate_limited", "Too many requests to the model just now.") from e
                cause = f" ({type(e.__cause__).__name__})" if e.__cause__ else ""
                raise ModelError("model_error", f"The model call failed: {e}{cause}") from e
            except a.APIError as e:
                # Name the underlying cause but never its message: for header errors that message contains the key.
                cause = f" ({type(e.__cause__).__name__})" if e.__cause__ else ""
                raise ModelError("model_error", f"The model call failed: {e}{cause}") from e
            log.append({"ms": int((time.monotonic() - t0) * 1000), "outcome": "ok", "effort": effort, "provider": self.provider,
                        "output_tokens": getattr(getattr(msg, "usage", None), "output_tokens", None)})
            text = "".join(getattr(b, "text", "") for b in msg.content)
            return parse_json(text), log
        raise ModelError("model_error", "The model call failed.")


class ClaudeVertexModel(AnthropicModel):
    """Claude through Vertex AI: same requests as the Anthropic API, signed in as the Cloud Run service account.
    POST_DESK_CLAUDE_VERTEX_MODEL sets the model and POST_DESK_CLAUDE_VERTEX_REGION the region (default global)."""

    def __init__(self, model: str | None = None, project: str | None = None, region: str | None = None, max_tokens: int = 8000):
        self._project = project or os.environ.get("GOOGLE_CLOUD_PROJECT")
        self._region = region or os.environ.get("POST_DESK_CLAUDE_VERTEX_REGION", "global")
        if not self._project:
            raise RuntimeError("GOOGLE_CLOUD_PROJECT isn't set, so Claude on Vertex AI can't be used.")
        super().__init__(model=model or os.environ.get("POST_DESK_CLAUDE_VERTEX_MODEL", "claude-sonnet-5-5"), api_key="unused",
                         max_tokens=max_tokens)
        self.provider = "claude-vertex"

    def _make_client(self, anthropic, key):
        self._anthropic_mod = anthropic
        return anthropic.AnthropicVertex(project_id=self._project, region=self._region, max_retries=0, timeout=self._timeout())


class GeminiModel:
    """Gemini through Vertex AI. Signs in as the Cloud Run service account, so there's no key to manage.

    Settings: POST_DESK_GEMINI_MODEL (default gemini-3.8-flash), POST_DESK_GEMINI_LOCATION (default global).
    Effort maps to Gemini's thinking level (low, medium, high). Replies are requested as JSON.
    Same retry policy and attempt records as the Claude client, so the two can be compared like for like."""

    ATTEMPT_TIMEOUT = AnthropicModel.ATTEMPT_TIMEOUT
    ATTEMPTS = AnthropicModel.ATTEMPTS

    def __init__(self, model: str | None = None, project: str | None = None, location: str | None = None,
                 max_tokens: int = 8000, client=None):
        self.provider = "gemini"
        self.name = model or os.environ.get("POST_DESK_GEMINI_MODEL", "gemini-3.8-flash")
        self.max_tokens = max_tokens
        self.effort = os.environ.get("POST_DESK_EFFORT", "low").strip() or "low"
        if client is None:
            from google import genai

            project = project or os.environ.get("GOOGLE_CLOUD_PROJECT")
            if not project:
                raise RuntimeError("GOOGLE_CLOUD_PROJECT isn't set, so Gemini on Vertex AI can't be used.")
            from google.genai import types as _t

            client = genai.Client(vertexai=True, project=project,
                                  location=location or os.environ.get("POST_DESK_GEMINI_LOCATION", "global"),
                                  http_options=_t.HttpOptions(httpx_client=connect_capped_httpx_client()))
        self._client = client

    def json(self, prompt: str) -> dict:
        return self.json_with_info(prompt)[0]

    def json_with_info(self, prompt: str, attempts: int | None = None, timeout: float | None = None,
                       effort: str | None = None) -> tuple[dict, list[dict]]:
        import httpx
        from google.genai import errors, types

        effort = effort or self.effort
        level = {"low": "LOW", "medium": "MEDIUM", "high": "HIGH"}.get(effort, "MEDIUM")
        config = types.GenerateContentConfig(
            response_mime_type="application/json",
            max_output_tokens=self.max_tokens,
            thinking_config=types.ThinkingConfig(thinking_level=level),
            http_options=types.HttpOptions(timeout=int((timeout or self.ATTEMPT_TIMEOUT) * 1000)),
        )
        tries = attempts or self.ATTEMPTS
        log: list[dict] = []
        for n in range(tries):
            t0 = time.monotonic()
            outcome, err = "ok", None
            try:
                r = self._client.models.generate_content(model=self.name, contents=prompt, config=config)
            except errors.APIError as e:
                err = e
                code = getattr(e, "code", None)
                outcome = "rate_limited" if code == 429 else "server_error" if code and code >= 500 else "client_error"
            except httpx.TimeoutException as e:
                err, outcome = e, "timeout"
            except httpx.TransportError as e:
                err, outcome = e, "connection_error"
            ms = int((time.monotonic() - t0) * 1000)
            if outcome == "client_error":   # wrong model name, no permission, bad request: retrying won't help
                log.append({"ms": ms, "outcome": outcome, "effort": effort, "provider": self.provider})
                raise ModelError("model_error", f"Gemini refused the request ({getattr(err, 'code', '?')}): "
                                                f"{getattr(err, 'message', err)}") from err
            if outcome != "ok":
                log.append({"ms": ms, "outcome": outcome, "effort": effort, "provider": self.provider})
                if n + 1 < tries:
                    time.sleep(2 if outcome == "rate_limited" else 0.5)
                    continue
                if outcome == "timeout":
                    raise ModelError("model_timeout", "The model took too long to answer. Please try again.") from err
                if outcome == "rate_limited":
                    raise ModelError("rate_limited", "Too many requests to the model just now.") from err
                raise ModelError("model_error", f"The Gemini call failed ({outcome}): {err}") from err
            usage = getattr(r, "usage_metadata", None)
            out_tokens = sum(getattr(usage, f, None) or 0 for f in ("candidates_token_count", "thoughts_token_count")) if usage else None
            log.append({"ms": ms, "outcome": "ok", "effort": effort, "provider": self.provider, "output_tokens": out_tokens})
            text = getattr(r, "text", None)
            if not text:
                raise ModelError("model_error", "Gemini returned an empty answer (it may have been blocked or cut off).")
            return parse_json(text), log
        raise ModelError("model_error", "The model call failed.")


class FallbackModel:
    """Tries the main model, and if it fails for any reason (down, slow, rate limited, bad reply) asks the fallback once.

    Made fresh for each request, because it remembers within a run: once the main model has failed, the rest of that
    run (the anchor fix-up) goes straight to the fallback. Timings stay well inside Cloud Run's 300 seconds:
    main call up to 2 x 45s, fallback 1 x 60s, anchor fix-up 1 x 60s.

    After a run, `provider` and `name` say which model actually answered, and `fell_back` says why, if it did."""

    PRIMARY_TIMEOUT = 45
    FALLBACK_TIMEOUT = 60

    def __init__(self, primary, fallback, force_failure: bool = False):
        self.primary, self.fallback = primary, fallback
        self.effort = getattr(primary, "effort", None)
        self.fell_back: str | None = None
        self._answered = primary
        self._force = force_failure   # testing only: behave as if the main model failed

    @property
    def provider(self):
        return getattr(self._answered, "provider", None)

    @property
    def name(self):
        return getattr(self._answered, "name", None)

    def json(self, prompt: str) -> dict:
        return self.json_with_info(prompt)[0]

    def json_with_info(self, prompt: str, attempts: int | None = None, timeout: float | None = None,
                       effort: str | None = None, fallback_timeout: float | None = None) -> tuple[dict, list[dict]]:
        log: list[dict] = []
        first_error = None
        if not self.fell_back:
            try:
                if self._force:
                    raise ModelError("model_error", "forced failure (testing the fallback)")
                out, log = self.primary.json_with_info(prompt, attempts=attempts,
                                                       timeout=min(timeout or self.PRIMARY_TIMEOUT, self.PRIMARY_TIMEOUT), effort=effort)
                self._answered = self.primary
                return out, log
            except ModelError as e:
                first_error = e
                self.fell_back = f"{getattr(self.primary, 'provider', 'main model')} {e.code}: {e}"
                log = [{"ms": 0, "outcome": "fell_back", "provider": getattr(self.primary, "provider", None), "reason": e.code}]
        try:
            out, more = self.fallback.json_with_info(prompt, attempts=1,
                                                     timeout=min(fallback_timeout or timeout or self.FALLBACK_TIMEOUT, self.FALLBACK_TIMEOUT),
                                                     effort=effort)
        except ModelError as e:
            why = f"{self.fell_back}; then {getattr(self.fallback, 'provider', 'fallback')} {e.code}: {e}"
            raise ModelError(first_error.code if first_error else e.code, f"Both models failed ({why}).") from e
        self._answered = self.fallback
        return out, log + more


PROVIDERS = {"anthropic": AnthropicModel, "gemini": GeminiModel, "claude-vertex": ClaudeVertexModel}


def make_model(provider: str) -> JSONModel:
    try:
        return PROVIDERS[provider]()
    except KeyError:
        raise RuntimeError(f"Unknown model provider: {provider!r} (choose from {', '.join(PROVIDERS)})")


def default_model() -> JSONModel:
    return make_model(os.environ.get("POST_DESK_PROVIDER", "anthropic").strip() or "anthropic")
