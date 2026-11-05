"""Model providers."""

from typing import Optional

from .base import VisionProvider
from .gemini import GeminiProvider


def create_provider(api_key: str, model_name: str, base_url: Optional[str] = None) -> VisionProvider:
    """Create the provider for the configured endpoint (the Gemini protocol)."""
    return GeminiProvider(api_key=api_key, model_name=model_name, base_url=base_url)


__all__ = ["VisionProvider", "GeminiProvider", "create_provider"]
