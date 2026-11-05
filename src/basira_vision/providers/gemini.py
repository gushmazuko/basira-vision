"""Gemini-protocol provider (Google API or any compatible gateway)."""

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
    VisionProviderError,
    handle_provider_error,
)
from ..utils.logging import get_logger
from .base import VisionProvider

logger = get_logger("providers.gemini")

# Transport-level knobs (no generation config is ever set on requests).
_REQUEST_TIMEOUT_S = 120
_MAX_ATTEMPTS = 3  # total attempts per request, retries owned here
_MAX_RETRY_DELAY_S = 60.0
_CHECK_TIMEOUT_S = 15  # connection check before a run
_RETRYABLE_API_CODES = {408, 429}  # plus 5xx, computed per error


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

    def _generate_with_retry(self, contents: Any) -> types.GenerateContentResponse:
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
    def extract_text(self, image_path: Path, prompt: str) -> str:
        """Read the text on a page image."""
        image = self.load_image(image_path)
        response = self._generate_with_retry([prompt, image])
        text = getattr(response, "text", None) or ""
        if not text:
            logger.debug(f"Empty OCR response for {image_path}")
        return text.strip()

    @handle_provider_error
    def generate_text(self, prompt: str) -> str:
        """Send a text-only prompt."""
        logger.debug(f"Prompt ({len(prompt)} chars): {prompt[:500]}{'...' if len(prompt) > 500 else ''}")
        response = self._generate_with_retry(prompt)
        return (getattr(response, "text", None) or "").strip()

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
