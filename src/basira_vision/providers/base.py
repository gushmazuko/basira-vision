"""Abstract base class for model providers."""

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Callable, Optional

from PIL import Image

from ..utils.errors import VisionProviderError

# on_retry(reason, attempt, max_retries, wait_seconds, rate_limited)
RetryCallback = Callable[[str, int, int, float, bool], None]


class VisionProvider(ABC):
    """A multimodal model that reads page images and generates text."""

    def __init__(self, name: str):
        self.name = name
        self.endpoint = ""
        # Called before every retry wait, so the caller can show it.
        self.on_retry: Optional[RetryCallback] = None

    @abstractmethod
    def extract_text(self, image_path: Path, prompt: str) -> str:
        """Return the text the model reads from a page image."""

    @abstractmethod
    def generate_text(self, prompt: str) -> str:
        """Return the model's reply to a text-only prompt."""

    @abstractmethod
    def check_connection(self) -> Optional[str]:
        """Check the endpoint before a run; return a warning or None, raise if unusable."""

    @staticmethod
    def load_image(image_path: Path) -> Image.Image:
        """Load an image fully into memory as RGB."""
        try:
            with Image.open(image_path) as image:
                return image.convert("RGB")
        except Exception as e:
            raise VisionProviderError(f"Failed to load image {image_path}: {e}") from e

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(name={self.name!r})"
