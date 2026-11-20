"""Gemini-protocol provider (Google API or any compatible gateway)."""

import io
import math
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlsplit

import httpx
from google import genai
from google.genai import errors, types

from ..utils.errors import (
    AuthenticationError,
    EndpointUnreachableError,
    RateLimitError,
    UnfinishedReplyError,
    VisionProviderError,
    handle_provider_error,
)
from ..utils.logging import get_logger
from .base import VisionProvider

logger = get_logger("providers.gemini")

# Transport-level knobs (no generation config is ever set on requests).
_REQUEST_TIMEOUT_S = 120
_HEAVY_REQUEST_TIMEOUT_S = 300  # OCR with strips or a raised media resolution
MEDIA_RESOLUTIONS = ("low", "medium", "high", "ultra_high")
THINKING_LEVELS = ("low", "medium", "high", "xhigh")
_MAX_ATTEMPTS = 3  # total attempts per request, retries owned here
_MAX_RETRY_DELAY_S = 60.0
_CHECK_TIMEOUT_S = 15  # connection check before a run
_RETRYABLE_API_CODES = {408, 429}  # plus 5xx, computed per error

# Appended to the OCR prompt when the page is sent together with its strips.
STRIPS_NOTE = (
    "\n\nIMAGES:\n"
    "- The first image is the whole page. Use it for the layout and structure: headings, paragraphs, reading order.\n"
    "- The next {count} images are horizontal strips of the same page at full resolution, from top to bottom. "
    "Neighboring strips overlap slightly.\n"
    "- Read every word letter by letter from the strips, where small letters and signs are clearest.\n"
    "- Transcribe the page once: a line that appears in two overlapping strips is written only once."
)


def _image_part(image: Any, media_resolution: str) -> types.Part:
    """An inline PNG part with its own media resolution (token budget)."""
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return types.Part(
        inline_data=types.Blob(data=buffer.getvalue(), mime_type="image/png"),
        media_resolution=types.PartMediaResolution(level=f"MEDIA_RESOLUTION_{media_resolution.upper()}"),
    )


def _reply_text(response: Any, blank_ok: bool) -> str:
    """The text of a model reply, or an error when the reply is not a finished one.

    A reply cut off (MAX_TOKENS) or stopped by the model (RECITATION, SAFETY, ...) fails
    even when it holds some text, so the page is recorded as failed and ``--resume`` retries
    it. A reply that finished normally (STOP) with no text is a blank page when ``blank_ok``.
    """
    text = (getattr(response, "text", None) or "").strip()
    candidates = getattr(response, "candidates", None) or []
    finish = getattr(candidates[0], "finish_reason", None) if candidates else None
    reason = str(getattr(finish, "name", finish) or "").upper()  # an enum, or any spelling from a gateway
    if not reason or reason == "FINISH_REASON_UNSPECIFIED":
        reason = None
    if reason == "MAX_TOKENS":
        raise UnfinishedReplyError("reply cut off: MAX_TOKENS")
    if reason not in (None, "STOP"):
        raise UnfinishedReplyError(f"reply stopped: {reason}")
    if text:
        return text
    block = getattr(getattr(response, "prompt_feedback", None), "block_reason", None)
    block = getattr(block, "name", block)
    if block and block != "BLOCKED_REASON_UNSPECIFIED":
        raise UnfinishedReplyError(f"request blocked: {block}")
    if reason == "STOP" and blank_ok:
        return ""
    raise UnfinishedReplyError("empty reply" + ("" if reason else " (no finish reason)"))


def _is_retryable_code(code: Any) -> bool:
    """Only request timeouts, rate limits and server errors are worth retrying."""
    if not isinstance(code, int):
        return False
    return code in _RETRYABLE_API_CODES or 500 <= code < 600


def _retry_delay(error: Exception, attempt: int) -> float:
    """Honor a Retry-After header when present; exponential backoff otherwise."""
    response = getattr(error, "response", None)
    headers = getattr(response, "headers", None) or {}
    value = headers.get("Retry-After")
    if value:
        try:
            seconds = float(value)
        except (TypeError, ValueError):
            try:
                when = parsedate_to_datetime(value)
                if when.tzinfo is None:
                    when = when.replace(tzinfo=timezone.utc)
                seconds = (when - datetime.now(timezone.utc)).total_seconds()
            except (TypeError, ValueError, OverflowError):
                seconds = float(2 ** attempt)
        if math.isfinite(seconds):
            return min(_MAX_RETRY_DELAY_S, max(0.0, seconds))
    return min(_MAX_RETRY_DELAY_S, float(2 ** attempt))


def _validate_base_url(base_url: str) -> str:
    """Accept a Gemini-protocol service root only: no API version or credentials."""
    parts = urlsplit(base_url)
    if (
        parts.scheme not in ("https", "http")
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
        or parts.path.rstrip("/").endswith(("/v1", "/v1beta", "/openai"))
    ):
        raise ValueError(
            "GEMINI_BASE_URL must be an HTTP(S) Gemini service root "
            "(no /v1, /v1beta or /openai suffix, no credentials)."
        )
    return base_url.rstrip("/")


def describe_api_error(error: errors.APIError) -> str:
    """``HTTP 500 INTERNAL: <first line of the message>``, without the JSON dump."""
    text = f"HTTP {error.code}"
    if error.status:
        text += f" {error.status}"
    message = (error.message or "").strip().splitlines()
    if message and message[0] and message[0] != error.status:
        first = message[0]
        text += f": {first[:120]}{'…' if len(first) > 120 else ''}"
    return text


def describe_transport_error(error: Exception, endpoint: str) -> str:
    """``connection refused (host:port)``-style summary of a network failure."""
    detail = str(error).lower()
    if isinstance(error, (httpx.TimeoutException, TimeoutError)) or "timed out" in detail:
        what = "timed out"
    elif "refused" in detail:
        what = "connection refused"
    elif "name or service not known" in detail or "nodename nor servname" in detail or "getaddrinfo" in detail:
        what = "host not found"
    elif isinstance(error, httpx.RemoteProtocolError) or "disconnected" in detail:
        what = "connection closed by the server"
    else:
        what = f"network error ({type(error).__name__})"
    return f"{what} ({endpoint})"


class GeminiProvider(VisionProvider):
    """Gemini-protocol provider for page OCR and translation."""

    def __init__(self, api_key: str, model_name: str, base_url: Optional[str] = None):
        """
        Initialize the Gemini provider.

        Args:
            api_key: Gemini API key (or gateway key when base_url is set)
            model_name: Gemini model name as served by the configured endpoint
            base_url: Optional Gemini-protocol service root (e.g. a gateway);
                defaults to the Google API endpoint

        Raises:
            ValueError: If base_url is not a valid Gemini service root
        """
        super().__init__(f"Gemini ({model_name})")
        self.model_name = model_name

        # Client creation makes no network call; check_connection() does.
        # SDK retries are disabled so this class stays the single retry owner.
        options: Dict[str, Any] = {
            "api_version": "v1beta",
            "timeout": _REQUEST_TIMEOUT_S * 1000,
            "retry_options": types.HttpRetryOptions(attempts=1),
        }
        if base_url:
            options["base_url"] = _validate_base_url(base_url)
        self.endpoint = urlsplit(options.get("base_url", "https://generativelanguage.googleapis.com")).netloc
        self.client = genai.Client(
            api_key=api_key,
            enterprise=False,
            http_options=types.HttpOptions(**options),
        )
        logger.debug(f"Initialized Gemini provider: model={model_name}, endpoint={self.endpoint}")

    def _generate_with_retry(self, contents: Any, timeout_s: Optional[int] = None,
                             thinking_level: Optional[str] = None) -> types.GenerateContentResponse:
        """
        Generate content, retrying only transport failures, 408/429 and 5xx.

        Authentication failures, invalid requests and undecodable responses
        fail immediately. Retry-After headers are honored on waits, and every
        wait is announced through ``on_retry``.

        Raises:
            AuthenticationError: On 401/403
            RateLimitError: On an exhausted 429
            VisionProviderError: On any other terminal failure
        """
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                options: Dict[str, Any] = {}
                if timeout_s:
                    options["http_options"] = types.HttpOptions(timeout=timeout_s * 1000)
                if thinking_level:
                    options["thinking_config"] = types.ThinkingConfig(thinking_level=thinking_level.upper())
                if options:
                    config = types.GenerateContentConfig(**options)
                    return self.client.models.generate_content(model=self.model_name, contents=contents, config=config)
                return self.client.models.generate_content(model=self.model_name, contents=contents)

            except errors.APIError as e:
                reason = describe_api_error(e)
                if e.code in (401, 403):
                    raise AuthenticationError(f"the endpoint rejected the API key ({reason})") from None
                if not _is_retryable_code(e.code) or attempt == _MAX_ATTEMPTS:
                    if e.code == 429:
                        raise RateLimitError(f"rate limit exceeded ({reason})") from None
                    raise VisionProviderError(reason) from None
                self._wait(reason, attempt, _retry_delay(e, attempt - 1), rate_limited=e.code == 429)

            except (httpx.TransportError, TimeoutError, ConnectionError) as e:
                reason = describe_transport_error(e, self.endpoint)
                if attempt == _MAX_ATTEMPTS:
                    raise VisionProviderError(reason) from None
                self._wait(reason, attempt, _retry_delay(e, attempt - 1))

        raise VisionProviderError("request failed after retries")

    def _wait(self, reason: str, attempt: int, delay: float, rate_limited: bool = False) -> None:
        logger.debug(f"{reason}; retry {attempt}/{_MAX_ATTEMPTS - 1} in {delay:.0f}s")
        if self.on_retry:
            self.on_retry(reason, attempt, _MAX_ATTEMPTS - 1, delay, rate_limited)
        time.sleep(delay)

    @handle_provider_error
    def extract_text(self, image_path: Path, prompt: str, strips: int = 0,
                     media_resolution: Optional[str] = None, thinking_level: Optional[str] = None) -> str:
        """Read the text on a page image, optionally with full-resolution strips of it."""
        image = self.load_image(image_path)
        images = [image, *self.split_strips(image, strips)] if strips > 0 else [image]
        if strips > 0:
            prompt += STRIPS_NOTE.format(count=strips)
        if media_resolution:
            images = [_image_part(im, media_resolution) for im in images]
        heavy = strips > 0 or media_resolution == "ultra_high"
        response = self._generate_with_retry([prompt, *images], _HEAVY_REQUEST_TIMEOUT_S if heavy else None,
                                             thinking_level)
        usage = getattr(response, "usage_metadata", None)
        if usage:
            logger.debug(f"usage {image_path.name}: prompt={usage.prompt_token_count} "
                         f"thoughts={usage.thoughts_token_count} output={usage.candidates_token_count}")
        return _reply_text(response, blank_ok=True)

    @handle_provider_error
    def generate_text(self, prompt: str) -> str:
        """Send a text-only prompt."""
        logger.debug(f"Prompt ({len(prompt)} chars): {prompt[:500]}{'...' if len(prompt) > 500 else ''}")
        response = self._generate_with_retry(prompt)
        return _reply_text(response, blank_ok=False)

    def check_connection(self) -> Optional[str]:
        """
        Ask the endpoint for the configured model before a run starts.

        Returns:
            None when the model answered, or a warning when the endpoint is
            reachable but the check was inconclusive (some gateways do not
            implement model lookup)

        Raises:
            EndpointUnreachableError: The endpoint cannot be reached at all
            AuthenticationError: The endpoint rejected the key
        """
        try:
            self.client.models.get(
                model=self.model_name,
                config=types.GetModelConfig(http_options=types.HttpOptions(timeout=_CHECK_TIMEOUT_S * 1000)),
            )
            return None
        except errors.APIError as e:
            reason = describe_api_error(e)
            if e.code in (401, 403):
                raise AuthenticationError(f"the endpoint rejected the API key ({reason})") from None
            if e.code == 404:
                return f"model {self.model_name!r} was not found on {self.endpoint} ({reason}); trying anyway"
            return f"could not check model {self.model_name!r} ({reason}); trying anyway"
        except (httpx.TransportError, TimeoutError, ConnectionError) as e:
            raise EndpointUnreachableError(describe_transport_error(e, self.endpoint)) from None
