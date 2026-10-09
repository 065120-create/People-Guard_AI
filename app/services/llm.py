"""Configurable language-model client.

Supported providers (choose with LLM_PROVIDER):
  mock               - no model; agents use rule-based logic + labelled simulated narrative
  gemini             - Google AI Studio Gemini API (free tier available)
  groq               - Groq (OpenAI-compatible, free tier available)
  openrouter         - OpenRouter (OpenAI-compatible, ':free' models available)
  openai_compatible  - any OpenAI-compatible endpoint (e.g. a local Ollama server)

All calls go through plain HTTPS with httpx; no provider SDKs are required, so
switching provider is a configuration change, not a code change.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import dataclass

import httpx

from app.config import Settings

log = logging.getLogger("peopleguard.llm")

PROVIDER_BASE_URLS = {
    "gemini": "https://generativelanguage.googleapis.com/v1beta",
    "groq": "https://api.groq.com/openai/v1",
    "openrouter": "https://openrouter.ai/api/v1",
}


class LLMError(Exception):
    """Base class. `user_message` is safe to show in the UI (never has secrets)."""

    def __init__(self, user_message: str, *, retryable: bool = False):
        super().__init__(user_message)
        self.user_message = user_message
        self.retryable = retryable


class LLMNotConfigured(LLMError):
    pass


class LLMRateLimited(LLMError):
    pass


class LLMMalformedResponse(LLMError):
    pass


@dataclass
class LLMResult:
    data: dict
    raw_text: str
    model: str
    provider: str
    latency_ms: int
    attempts: int


def extract_json(text: str) -> dict:
    """Parse a JSON object from model output, tolerating code fences or prose."""
    if not text or not text.strip():
        raise LLMMalformedResponse("The model returned an empty response.")
    cleaned = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", cleaned, re.S)
    if fence:
        cleaned = fence.group(1).strip()
    try:
        obj = json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start == -1 or end <= start:
            raise LLMMalformedResponse("The model response did not contain a JSON object.")
        try:
            obj = json.loads(cleaned[start : end + 1])
        except json.JSONDecodeError as exc:
            raise LLMMalformedResponse(f"The model returned malformed JSON ({exc.msg}).") from exc
    if not isinstance(obj, dict):
        raise LLMMalformedResponse("The model returned JSON that is not an object.")
    return obj


def _redact_secrets(text: str, settings: Settings) -> str:
    key = settings.api_key
    if key and key in text:
        text = text.replace(key, "***")
    return re.sub(r"(key=)[A-Za-z0-9_\-]+", r"\1***", text)


class LLMClient:
    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None):
        self.settings = settings
        self._transport = transport  # tests inject a mock transport here
        self._last_call = 0.0
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self.settings.llm_enabled

    @property
    def label(self) -> str:
        return f"{self.settings.llm_provider} / {self.settings.model}"

    # -- public ---------------------------------------------------------------
    def complete_json(self, system: str, user: str) -> LLMResult:
        if not self.enabled:
            status = self.settings.llm_status()
            raise LLMNotConfigured(status["message"])
        attempts, start = 0, time.monotonic()
        last_error: LLMError | None = None
        strict_user = user
        for attempt in range(self.settings.llm_max_retries + 1):
            attempts += 1
            self._pace()
            try:
                text = self._call(system, strict_user)
                data = extract_json(text)
                return LLMResult(
                    data=data,
                    raw_text=text,
                    model=self.settings.model,
                    provider=self.settings.llm_provider,
                    latency_ms=int((time.monotonic() - start) * 1000),
                    attempts=attempts,
                )
            except LLMMalformedResponse as exc:
                last_error = exc
                strict_user = user + "\n\nIMPORTANT: Your previous reply was not valid JSON. Reply with ONE valid JSON object only, no prose, no code fences."
            except LLMRateLimited as exc:
                last_error = exc
                ra = getattr(exc, "retry_after", None)
                wait = ra if ra is not None else min(4 * (attempt + 1), 20)
                if attempt < self.settings.llm_max_retries:
                    time.sleep(min(float(wait), 20.0))
            except LLMError as exc:
                last_error = exc
                if not exc.retryable:
                    break
                if attempt < self.settings.llm_max_retries:
                    time.sleep(min(2 * (attempt + 1), 8))
        assert last_error is not None
        raise last_error

    # -- internals ------------------------------------------------------------
    def _pace(self) -> None:
        with self._lock:
            gap = self.settings.llm_min_interval_seconds - (time.monotonic() - self._last_call)
            if gap > 0 and self._last_call:
                time.sleep(gap)
            self._last_call = time.monotonic()

    def _client(self) -> httpx.Client:
        return httpx.Client(timeout=self.settings.llm_timeout_seconds, transport=self._transport)

    def _call(self, system: str, user: str) -> str:
        provider = self.settings.llm_provider
        try:
            if provider == "gemini":
                return self._call_gemini(system, user)
            return self._call_openai_compatible(system, user)
        except httpx.TimeoutException as exc:
            raise LLMError("The language model request timed out.", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise LLMError(f"Could not reach the language model provider ({type(exc).__name__}).", retryable=True) from exc

    def _raise_for_status(self, resp: httpx.Response) -> None:
        if resp.status_code < 400:
            return
        body = _redact_secrets(resp.text[:300], self.settings)
        if resp.status_code == 429:
            err = LLMRateLimited("The provider's rate limit or free-tier quota was reached. Wait a minute and retry, or switch provider/model.", retryable=True)
            ra = resp.headers.get("retry-after")
            try:
                err.retry_after = float(ra) if ra else None  # type: ignore[attr-defined]
            except ValueError:
                err.retry_after = None  # type: ignore[attr-defined]
            raise err
        if resp.status_code in (401, 403):
            raise LLMError(f"The provider rejected the API key (HTTP {resp.status_code}). Check {self.settings.api_key_env_name}.")
        if resp.status_code == 404:
            raise LLMError(f"Model '{self.settings.model}' was not found for provider '{self.settings.llm_provider}'. Check LLM_MODEL.")
        if resp.status_code >= 500:
            raise LLMError(f"The provider returned a server error (HTTP {resp.status_code}).", retryable=True)
        log.warning("LLM HTTP %s: %s", resp.status_code, body)
        raise LLMError(f"The provider returned HTTP {resp.status_code}.")

    def _call_gemini(self, system: str, user: str) -> str:
        base = self.settings.llm_base_url or PROVIDER_BASE_URLS["gemini"]
        url = f"{base}/models/{self.settings.model}:generateContent"
        payload = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {"temperature": self.settings.llm_temperature, "responseMimeType": "application/json"},
        }
        with self._client() as client:
            resp = client.post(url, json=payload, headers={"x-goog-api-key": self.settings.api_key})
        self._raise_for_status(resp)
        try:
            data = resp.json()
            candidates = data.get("candidates") or []
            if not candidates:
                reason = (data.get("promptFeedback") or {}).get("blockReason")
                raise LLMMalformedResponse(f"The model returned no content{f' (blocked: {reason})' if reason else ''}.")
            parts = candidates[0].get("content", {}).get("parts", [])
            return "".join(p.get("text", "") for p in parts)
        except ValueError as exc:
            raise LLMMalformedResponse("The provider response was not valid JSON.") from exc

    def _call_openai_compatible(self, system: str, user: str) -> str:
        provider = self.settings.llm_provider
        base = self.settings.llm_base_url or PROVIDER_BASE_URLS.get(provider, "")
        if not base:
            raise LLMNotConfigured("LLM_BASE_URL must be set for the openai_compatible provider.")
        url = base.rstrip("/") + "/chat/completions"
        headers = {"Content-Type": "application/json"}
        if self.settings.api_key:
            headers["Authorization"] = f"Bearer {self.settings.api_key}"
        if provider == "openrouter":
            headers["X-Title"] = "PeopleGuard AI (academic prototype)"
        payload = {
            "model": self.settings.model,
            "temperature": self.settings.llm_temperature,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "response_format": {"type": "json_object"},
        }
        with self._client() as client:
            resp = client.post(url, json=payload, headers=headers)
            if resp.status_code == 400 and "response_format" in resp.text:
                # Some models do not support JSON mode; retry without it.
                payload.pop("response_format")
                resp = client.post(url, json=payload, headers=headers)
        self._raise_for_status(resp)
        try:
            data = resp.json()
            return data["choices"][0]["message"]["content"] or ""
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise LLMMalformedResponse("The provider response did not have the expected chat-completion shape.") from exc
