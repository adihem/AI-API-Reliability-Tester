"""Talk to an AI API and normalize every outcome into an APIResponse.

This module only communicates. Validation and retry decisions live elsewhere.
"""

import logging
import os
import time
from typing import Protocol

import httpx
from dotenv import load_dotenv

from tester.models import APIResponse, FailureType, ModelConfig, RequestConfig

log = logging.getLogger(__name__)

MAX_ERROR_CHARS = 500


class APIClient(Protocol):
    """What the runner depends on. Tests pass fakes; providers implement this."""

    def send(self, prompt: str) -> APIResponse: ...


class MissingAPIKey(Exception):
    pass


def classify_status(status: int) -> FailureType:
    if status in (401, 403):
        return FailureType.AUTHENTICATION_ERROR
    if status == 429:
        return FailureType.RATE_LIMIT
    if status == 408:
        return FailureType.TIMEOUT
    if 400 <= status < 500:
        return FailureType.BAD_REQUEST
    if status >= 500:
        return FailureType.SERVER_ERROR
    return FailureType.UNKNOWN_ERROR


class OpenAICompatibleClient:
    """POST {base_url}/chat/completions, the format shared by OpenAI, Groq, OpenRouter, Ollama, ..."""

    def __init__(
        self,
        model: ModelConfig,
        request: RequestConfig,
        api_key: str,
        transport: httpx.BaseTransport | None = None,
    ):
        self.model = model
        self.request = request
        self._api_key = api_key
        self._http = httpx.Client(
            base_url=model.base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=request.timeout_seconds,
            transport=transport,
        )

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self) -> None:
        self._http.close()

    def send(self, prompt: str) -> APIResponse:
        payload = {
            "model": self.model.name,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": self.request.temperature,
            "max_tokens": self.request.max_tokens,
        }
        start = time.perf_counter()
        try:
            resp = self._http.post("chat/completions", json=payload)
        except httpx.TimeoutException as e:
            return self._fail(start, FailureType.TIMEOUT, f"{type(e).__name__}: {e}")
        except httpx.TransportError as e:
            return self._fail(start, FailureType.CONNECTION_ERROR, f"{type(e).__name__}: {e}")
        latency = time.perf_counter() - start
        log.debug("model=%s status=%s latency=%.3fs", self.model.name, resp.status_code, latency)

        if not resp.is_success:
            return self._fail(start, classify_status(resp.status_code), _error_text(resp), resp.status_code, latency)

        try:
            body = resp.json()
            content = body["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as e:
            return self._fail(start, FailureType.INVALID_RESPONSE,
                              f"unexpected response shape ({type(e).__name__}): {resp.text}", resp.status_code, latency)

        usage = body.get("usage") or {}
        tokens = dict(
            input_tokens=usage.get("prompt_tokens"),
            output_tokens=usage.get("completion_tokens"),
            total_tokens=usage.get("total_tokens"),
        )
        if not isinstance(content, str) or not content.strip():
            return APIResponse(success=False, text=content if isinstance(content, str) else None,
                               http_status=resp.status_code, latency_seconds=latency,
                               error_type=FailureType.EMPTY_RESPONSE, error_message="empty message content", **tokens)

        return APIResponse(success=True, text=content, http_status=resp.status_code,
                           latency_seconds=latency, **tokens)

    def _fail(self, start, error_type, message, status=None, latency=None) -> APIResponse:
        if latency is None:
            latency = time.perf_counter() - start
        message = message.replace(self._api_key, "***")[:MAX_ERROR_CHARS]
        return APIResponse(success=False, http_status=status, latency_seconds=latency,
                           error_type=error_type, error_message=message)


def _error_text(resp: httpx.Response) -> str:
    try:
        err = resp.json().get("error")
        if isinstance(err, dict) and err.get("message"):
            msg = f"HTTP {resp.status_code}: {err['message']}"
            raw = (err.get("metadata") or {}).get("raw")
            return f"{msg} | {raw}" if raw else msg
    except (ValueError, AttributeError):
        pass
    return f"HTTP {resp.status_code}: {resp.text}"


def load_api_key(env_name: str) -> str:
    load_dotenv()
    key = os.environ.get(env_name, "").strip()
    if not key:
        raise MissingAPIKey(f"Environment variable {env_name} is not set. Add it to .env (see .env.example).")
    return key


def build_client(model: ModelConfig, request: RequestConfig) -> OpenAICompatibleClient:
    return OpenAICompatibleClient(model, request, load_api_key(model.api_key_env))
