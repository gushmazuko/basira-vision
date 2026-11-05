from types import SimpleNamespace

import httpx
import pytest
from google.genai import errors

from basira_vision.providers import gemini
from basira_vision.providers.gemini import (
    GeminiProvider,
    _is_retryable_code,
    _retry_delay,
    _validate_base_url,
    describe_api_error,
    describe_transport_error,
)
from basira_vision.utils.errors import (
    AuthenticationError,
    EndpointUnreachableError,
    RateLimitError,
    VisionProviderError,
)


@pytest.mark.parametrize("code", [408, 429, 500, 503])
def test_retryable_codes(code):
    assert _is_retryable_code(code)


@pytest.mark.parametrize("code", [400, 401, 403, 404, None, "500"])
def test_non_retryable_codes(code):
    assert not _is_retryable_code(code)


def test_retry_delay_honors_retry_after_and_caps_it():
    def error(value):
        return SimpleNamespace(response=SimpleNamespace(headers={"Retry-After": value}))

    assert _retry_delay(error("7"), 0) == 7
    assert _retry_delay(error("9999"), 0) == 60
    assert _retry_delay(SimpleNamespace(), 2) == 4


@pytest.mark.parametrize("url", ["https://gw.local:8080", "http://gw.local/", "https://gw.local/prefix"])
def test_valid_base_urls(url):
    assert _validate_base_url(url) == url.rstrip("/")


@pytest.mark.parametrize(
    "url",
    ["https://gw.local/v1", "https://gw.local/v1beta/", "https://gw.local/openai",
     "ftp://gw.local", "https://user:pw@gw.local", "https://gw.local?key=1"],
)
def test_invalid_base_urls(url):
    with pytest.raises(ValueError):
        _validate_base_url(url)


# ---------------------------------------------------------------------------- provider behavior


def api_error(code, status, message="simulated"):
    return errors.APIError(code, {"error": {"code": code, "message": message, "status": status}})


def test_api_errors_are_described_without_the_json_dump():
    described = describe_api_error(api_error(500, "INTERNAL", "simulated INTERNAL"))
    assert described == "HTTP 500 INTERNAL: simulated INTERNAL"
    assert describe_api_error(api_error(503, "UNAVAILABLE", "UNAVAILABLE")) == "HTTP 503 UNAVAILABLE"
    long = describe_api_error(api_error(400, "INVALID_ARGUMENT", "x" * 300 + "\nsecond line"))
    assert long.endswith("…") and "second line" not in long


def test_transport_errors_are_described_with_the_endpoint():
    assert describe_transport_error(httpx.ConnectError("[Errno 61] Connection refused"), "gw:1") == \
        "connection refused (gw:1)"
    assert describe_transport_error(httpx.ReadTimeout("read timed out"), "gw:1") == "timed out (gw:1)"
    assert describe_transport_error(httpx.ConnectError("[Errno 8] nodename nor servname provided"), "gw:1") == \
        "host not found (gw:1)"


class StubModels:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    def _next(self):
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def generate_content(self, model, contents):
        return self._next()

    def get(self, model, config=None):
        return self._next()


def provider_with(outcomes, monkeypatch):
    monkeypatch.setattr(gemini.time, "sleep", lambda seconds: None)
    provider = GeminiProvider(api_key="k", model_name="m", base_url="http://gw.local:8080")
    provider.client = SimpleNamespace(models=StubModels(outcomes))
    return provider


def test_retries_are_announced_then_succeed(monkeypatch):
    provider = provider_with([api_error(500, "INTERNAL"), httpx.ConnectError("refused"),
                              SimpleNamespace(text=" done ")], monkeypatch)
    seen = []
    provider.on_retry = lambda *args: seen.append(args)
    assert provider.generate_text("hi") == "done"
    assert [(reason, attempt, attempts) for reason, attempt, attempts, _, _ in seen] == [
        ("HTTP 500 INTERNAL: simulated", 1, 2),
        ("connection refused (gw.local:8080)", 2, 2),
    ]


def test_rate_limit_retries_are_flagged_and_exhaustion_raises(monkeypatch):
    provider = provider_with([api_error(429, "RESOURCE_EXHAUSTED")] * 3, monkeypatch)
    seen = []
    provider.on_retry = lambda *args: seen.append(args[-1])
    with pytest.raises(RateLimitError):
        provider.generate_text("hi")
    assert seen == [True, True]


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        (api_error(401, "UNAUTHENTICATED"), AuthenticationError),
        (api_error(400, "INVALID_ARGUMENT"), VisionProviderError),
    ],
)
def test_non_retryable_errors_fail_at_once(monkeypatch, outcome, expected):
    provider = provider_with([outcome], monkeypatch)
    with pytest.raises(expected):
        provider.generate_text("hi")
    assert provider.client.models.calls == 1


def test_connection_check(monkeypatch):
    assert provider_with([SimpleNamespace()], monkeypatch).check_connection() is None
    warning = provider_with([api_error(404, "NOT_FOUND")], monkeypatch).check_connection()
    assert "was not found on gw.local:8080" in warning and "trying anyway" in warning
    with pytest.raises(EndpointUnreachableError, match="connection refused"):
        provider_with([httpx.ConnectError("[Errno 61] Connection refused")], monkeypatch).check_connection()
    with pytest.raises(AuthenticationError):
        provider_with([api_error(403, "PERMISSION_DENIED")], monkeypatch).check_connection()
