"""Exception hierarchy for Basira Vision."""

import functools


class BasiraError(Exception):
    """Base class for all Basira Vision errors."""


class ConfigurationError(BasiraError):
    """Invalid or missing configuration."""


class VisionProviderError(BasiraError):
    """A model provider request failed."""


class AuthenticationError(VisionProviderError):
    """The provider rejected the credentials."""


class RateLimitError(VisionProviderError):
    """The provider kept rate-limiting after all retries."""


class EndpointUnreachableError(VisionProviderError):
    """The model endpoint cannot be reached at all."""


class PDFProcessingError(BasiraError):
    """A PDF could not be opened or rendered."""


class PromptLoadingError(BasiraError):
    """A prompt template is missing or unusable."""


def handle_provider_error(func):
    """Wrap unexpected exceptions from a provider call in VisionProviderError."""

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except VisionProviderError:
            raise
        except Exception as e:
            raise VisionProviderError(f"Provider operation failed: {e}") from e

    return wrapper
