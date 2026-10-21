"""API client tests against a fake server (httpx.MockTransport). No real network calls."""

import json

import httpx
import pytest

from tester.api_client import MissingAPIKey, OpenAICompatibleClient, load_api_key
from tester.models import FailureType, ModelConfig, RequestConfig

KEY = "sk-test-secret-123"
MODEL = ModelConfig(name="fake-model", base_url="https://fake.test/v1")
REQUEST = RequestConfig(temperature=0.2, max_tokens=50, timeout_seconds=5)


def client(handler):
    return OpenAICompatibleClient(MODEL, REQUEST, KEY, transport=httpx.MockTransport(handler))


def ok_body(content="BUG", usage=True):
    body = {"choices": [{"message": {"role": "assistant", "content": content}}]}
    if usage:
        body["usage"] = {"prompt_tokens": 12, "completion_tokens": 1, "total_tokens": 13}
    return body


def reply(status=200, body=None, text=None):
    def handler(request):
        if text is not None:
            return httpx.Response(status, text=text)
        return httpx.Response(status, json=body)
    return handler


def raise_(exc):
    def handler(request):
        raise exc("boom", request=request)
    return handler


def test_success_parses_text_and_tokens():
    r = client(reply(body=ok_body())).send("hi")
    assert r.success and r.text == "BUG" and r.http_status == 200
    assert (r.input_tokens, r.output_tokens, r.total_tokens) == (12, 1, 13)
    assert r.latency_seconds >= 0 and r.error_type is None


def test_request_shape():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=ok_body())

    client(handler).send("Classify this")
    assert seen["url"] == "https://fake.test/v1/chat/completions"
    assert seen["auth"] == f"Bearer {KEY}"
    assert seen["body"] == {
        "model": "fake-model",
        "messages": [{"role": "user", "content": "Classify this"}],
        "temperature": 0.2,
        "max_tokens": 50,
    }


def test_missing_usage_gives_none_tokens():
    r = client(reply(body=ok_body(usage=False))).send("hi")
    assert r.success and r.total_tokens is None


@pytest.mark.parametrize(
    "exc,expected",
    [
        (httpx.ReadTimeout, FailureType.TIMEOUT),
        (httpx.ConnectTimeout, FailureType.TIMEOUT),
        (httpx.ConnectError, FailureType.CONNECTION_ERROR),
        (httpx.RemoteProtocolError, FailureType.CONNECTION_ERROR),
    ],
)
def test_transport_errors(exc, expected):
    r = client(raise_(exc)).send("hi")
    assert not r.success and r.error_type is expected and r.http_status is None
    assert exc.__name__ in r.error_message


@pytest.mark.parametrize(
    "status,expected",
    [
        (400, FailureType.BAD_REQUEST),
        (401, FailureType.AUTHENTICATION_ERROR),
        (403, FailureType.AUTHENTICATION_ERROR),
        (404, FailureType.BAD_REQUEST),
        (408, FailureType.TIMEOUT),
        (429, FailureType.RATE_LIMIT),
        (500, FailureType.SERVER_ERROR),
        (503, FailureType.SERVER_ERROR),
    ],
)
def test_http_errors(status, expected):
    r = client(reply(status, {"error": {"message": "nope"}})).send("hi")
    assert not r.success and r.error_type is expected and r.http_status == status
    assert r.error_message == f"HTTP {status}: nope"


def test_http_error_includes_upstream_detail():
    body = {"error": {"message": "Provider returned error", "metadata": {"raw": "upstream rate-limited"}}}
    r = client(reply(429, body)).send("hi")
    assert r.error_message == "HTTP 429: Provider returned error | upstream rate-limited"


def test_http_error_with_non_json_body():
    r = client(reply(502, text="<html>Bad Gateway</html>")).send("hi")
    assert r.error_type is FailureType.SERVER_ERROR and "Bad Gateway" in r.error_message


@pytest.mark.parametrize(
    "kwargs",
    [
        {"text": "not json"},
        {"body": {"choices": []}},
        {"body": {"result": "BUG"}},
        {"body": {"choices": [{"message": {}}]}},
    ],
)
def test_invalid_response(kwargs):
    r = client(reply(**kwargs)).send("hi")
    assert r.error_type is FailureType.INVALID_RESPONSE and r.http_status == 200


@pytest.mark.parametrize("content", ["", "   \n", None])
def test_empty_response(content):
    r = client(reply(body=ok_body(content))).send("hi")
    assert r.error_type is FailureType.EMPTY_RESPONSE and r.total_tokens == 13


def test_api_key_redacted_from_errors():
    r = client(reply(401, {"error": {"message": f"Incorrect API key provided: {KEY}"}})).send("hi")
    assert KEY not in r.error_message and "***" in r.error_message


def test_load_api_key(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MY_KEY", "abc")
    assert load_api_key("MY_KEY") == "abc"
    monkeypatch.delenv("MY_KEY")
    with pytest.raises(MissingAPIKey, match="MY_KEY"):
        load_api_key("MY_KEY")
