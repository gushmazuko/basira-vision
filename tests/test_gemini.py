from types import SimpleNamespace

import httpx
import pytest
from google.genai import errors, types
from PIL import Image

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
    UnfinishedReplyError,
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


def test_split_strips_cover_page_with_overlap():
    from PIL import Image

    from basira_vision.providers.base import VisionProvider

    page = Image.new("RGB", (100, 300), "white")
    strips = VisionProvider.split_strips(page, 3)
    assert [s.size for s in strips] == [(100, 110), (100, 120), (100, 110)]
    # first starts at the top, last ends at the bottom, neighbors overlap
    boxes = [(0, 110), (90, 210), (190, 300)]
    for strip, (top, bottom) in zip(strips, boxes, strict=True):
        assert strip.height == bottom - top


# ---------------------------------------------------------------------------- unfinished replies


def reply(text="", finish="STOP"):
    content = types.Content(parts=[types.Part(text=text)]) if text else None
    return types.GenerateContentResponse(candidates=[types.Candidate(finish_reason=finish, content=content)])


@pytest.fixture
def page_image(tmp_path):
    path = tmp_path / "page_1.png"
    Image.new("RGB", (20, 30), "white").save(path)
    return path


def test_a_finished_reply_is_returned_trimmed(monkeypatch, page_image):
    provider = provider_with([reply(" text of the page \n")], monkeypatch)
    assert provider.extract_text(page_image, "read") == "text of the page"


def test_a_finished_empty_reply_is_a_blank_page(monkeypatch, page_image):
    provider = provider_with([reply("")], monkeypatch)
    assert provider.extract_text(page_image, "read") == ""


@pytest.mark.parametrize("finish", ["RECITATION", "SAFETY", "PROHIBITED_CONTENT", "OTHER"])
def test_a_blocked_reply_fails_the_page_with_its_finish_reason(monkeypatch, page_image, finish):
    provider = provider_with([reply("", finish)], monkeypatch)
    with pytest.raises(VisionProviderError, match=finish):
        provider.extract_text(page_image, "read")
    assert provider.client.models.calls == 1


def test_a_reply_that_stopped_halfway_fails_the_page(monkeypatch, page_image):
    with pytest.raises(VisionProviderError, match="MAX_TOKENS"):
        provider_with([reply("half of the pa", "MAX_TOKENS")], monkeypatch).extract_text(page_image, "read")
    with pytest.raises(VisionProviderError, match="RECITATION"):
        provider_with([reply("half of the pa", "RECITATION")], monkeypatch).extract_text(page_image, "read")


def test_a_blocked_request_fails_the_page(monkeypatch, page_image):
    blocked = types.GenerateContentResponse(
        prompt_feedback=types.GenerateContentResponsePromptFeedback(block_reason="SAFETY"))
    with pytest.raises(VisionProviderError, match="SAFETY"):
        provider_with([blocked], monkeypatch).extract_text(page_image, "read")


def test_an_empty_reply_without_a_finish_reason_fails_the_page(monkeypatch, page_image):
    for response in (SimpleNamespace(text=""), types.GenerateContentResponse(candidates=[])):
        with pytest.raises(VisionProviderError, match="empty reply"):
            provider_with([response], monkeypatch).extract_text(page_image, "read")


def test_a_reply_without_a_finish_reason_but_with_text_is_accepted(monkeypatch, page_image):
    assert provider_with([SimpleNamespace(text="text")], monkeypatch).extract_text(page_image, "read") == "text"


def test_an_empty_translation_is_never_a_finished_page(monkeypatch):
    assert provider_with([reply("tr")], monkeypatch).generate_text("hi") == "tr"
    with pytest.raises(VisionProviderError, match="empty reply"):
        provider_with([reply("")], monkeypatch).generate_text("hi")
    with pytest.raises(VisionProviderError, match="MAX_TOKENS"):
        provider_with([reply("tr", "MAX_TOKENS")], monkeypatch).generate_text("hi")


def test_unusual_gateway_values_are_read_as_no_finish_reason(monkeypatch, page_image):
    for finish in ("", "FINISH_REASON_UNSPECIFIED", None):
        response = SimpleNamespace(text="text", candidates=[SimpleNamespace(finish_reason=finish)])
        assert provider_with([response], monkeypatch).extract_text(page_image, "read") == "text"
        empty = SimpleNamespace(text="", candidates=[SimpleNamespace(finish_reason=finish)])
        with pytest.raises(VisionProviderError, match="empty reply"):
            provider_with([empty], monkeypatch).extract_text(page_image, "read")


def test_a_block_reason_beats_a_blank_page(monkeypatch, page_image):
    blocked = types.GenerateContentResponse(
        candidates=[types.Candidate(finish_reason="STOP")],
        prompt_feedback=types.GenerateContentResponsePromptFeedback(block_reason="SAFETY"))
    with pytest.raises(VisionProviderError, match="SAFETY"):
        provider_with([blocked], monkeypatch).extract_text(page_image, "read")
    unspecified = SimpleNamespace(
        text="", prompt_feedback=SimpleNamespace(block_reason=SimpleNamespace(name="BLOCKED_REASON_UNSPECIFIED")))
    with pytest.raises(VisionProviderError, match="empty reply"):
        provider_with([unspecified], monkeypatch).extract_text(page_image, "read")


def test_unfinished_replies_are_their_own_error_kind(monkeypatch, page_image):
    with pytest.raises(UnfinishedReplyError):
        provider_with([reply("", "RECITATION")], monkeypatch).extract_text(page_image, "read")
    assert not issubclass(AuthenticationError, UnfinishedReplyError)


def test_finish_reasons_are_read_in_any_case(monkeypatch, page_image):
    lower = SimpleNamespace(text="text", candidates=[SimpleNamespace(finish_reason="stop")])
    assert provider_with([lower], monkeypatch).extract_text(page_image, "read") == "text"
    cut = SimpleNamespace(text="half", candidates=[SimpleNamespace(finish_reason="max_tokens")])
    with pytest.raises(UnfinishedReplyError, match="MAX_TOKENS"):
        provider_with([cut], monkeypatch).extract_text(page_image, "read")


def test_token_usage_is_logged_for_the_log_file_and_a_prompt_never_with_its_text(monkeypatch, page_image, caplog):
    def with_usage(text):
        response = reply(text)
        response.usage_metadata = SimpleNamespace(prompt_token_count=1200, thoughts_token_count=30,
                                                  candidates_token_count=450)
        return response

    provider = provider_with([with_usage("page text"), with_usage("translated")], monkeypatch)
    with caplog.at_level("DEBUG", logger="basira_vision"):
        provider.extract_text(page_image, "read the page")
        provider.generate_text("Translate this: THE TEXT OF THE PAGE")
    records = [(record.levelname, record.getMessage()) for record in caplog.records
               if record.name.endswith("gemini") and "Initialized" not in record.getMessage()]
    assert records == [
        ("INFO", f"usage {page_image.name}: prompt=1200 thoughts=30 output=450"),
        ("DEBUG", "Prompt: 36 chars"),
        ("INFO", "usage text: prompt=1200 thoughts=30 output=450"),
    ]
