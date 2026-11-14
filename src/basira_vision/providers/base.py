"""Abstract base class for model providers."""

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Callable, List, Optional

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
    def extract_text(self, image_path: Path, prompt: str, strips: int = 0,
                     media_resolution: Optional[str] = None, thinking_level: Optional[str] = None) -> str:
        """Return the text the model reads from a page image.

        With ``strips`` > 0 the model also gets that many overlapping
        horizontal strips of the page at full resolution, in the same request.
        ``media_resolution`` (low, medium, high, ultra_high) sets the token
        budget the model spends on each image; None keeps the model default.
        ``thinking_level`` (low, medium, high, xhigh) sets the reasoning effort;
        None keeps the model default.
        """

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

    @staticmethod
    def split_strips(image: Image.Image, count: int, overlap: float = 0.1) -> List[Image.Image]:
        """Cut a page into ``count`` full-width horizontal strips that overlap by ``overlap`` of a strip."""
        width, height = image.size
        step = height / count
        margin = int(step * overlap)
        return [
            image.crop((0, max(0, int(i * step) - margin), width, min(height, int((i + 1) * step) + margin)))
            for i in range(count)
        ]

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(name={self.name!r})"
