"""PDF page rendering."""

from pathlib import Path
from typing import List, Optional

import pymupdf

from ..utils.errors import PDFProcessingError
from ..utils.logging import get_logger

logger = get_logger("pdf")

IMAGE_FORMATS = ("PNG", "JPEG", "TIFF")


def page_count(pdf_path: Path) -> int:
    """Number of pages in a PDF."""
    try:
        with pymupdf.open(pdf_path) as doc:
            return len(doc)
    except Exception as e:
        raise PDFProcessingError(f"Cannot open PDF {pdf_path}: {e}") from e


def parse_page_range(value: Optional[str], total_pages: int) -> List[int]:
    """
    Turn a page range like ``"5"`` or ``"1-10"`` into page numbers.

    The end is clamped to the document length; everything else that does not
    describe at least one existing page is rejected.

    Raises:
        ValueError: If the range is malformed or outside the document
    """
    if not value:
        return list(range(1, total_pages + 1))

    start_str, sep, end_str = value.strip().partition("-")
    try:
        start = int(start_str)
        end = int(end_str) if sep else start
    except ValueError:
        raise ValueError(f"Invalid page range '{value}': use N or N-M, e.g. 5 or 1-10") from None

    if start < 1 or end < start:
        raise ValueError(f"Invalid page range '{value}': pages start at 1 and the end must not precede the start")
    if start > total_pages:
        raise ValueError(f"Page range '{value}' starts after the last page ({total_pages})")
    return list(range(start, min(end, total_pages) + 1))


class PDFRenderer:
    """Render single PDF pages to image files."""

    def __init__(self, pdf_path: Path, dpi: int = 300, image_format: str = "PNG"):
        image_format = image_format.upper()
        if image_format not in IMAGE_FORMATS:
            raise PDFProcessingError(f"Unsupported image format: {image_format}")
        self.pdf_path = pdf_path
        self.dpi = dpi
        self.image_format = image_format
        self._doc = None

    def __enter__(self) -> "PDFRenderer":
        try:
            self._doc = pymupdf.open(self.pdf_path)
        except Exception as e:
            raise PDFProcessingError(f"Cannot open PDF {self.pdf_path}: {e}") from e
        return self

    def __exit__(self, *exc) -> None:
        if self._doc is not None:
            self._doc.close()
            self._doc = None

    def render(self, page_number: int, output_dir: Path) -> Path:
        """Render a 1-indexed page to ``output_dir/page_NNN.<ext>``."""
        if self._doc is None:
            raise PDFProcessingError("PDFRenderer must be used as a context manager")
        output_dir.mkdir(parents=True, exist_ok=True)
        extension = "jpg" if self.image_format == "JPEG" else self.image_format.lower()
        image_path = output_dir / f"page_{page_number:03d}.{extension}"
        try:
            pixmap = self._doc[page_number - 1].get_pixmap(dpi=self.dpi, alpha=False)
            pil_image = pixmap.pil_image()
            options = {"quality": 95} if self.image_format == "JPEG" else {}
            pil_image.save(image_path, format=self.image_format, **options)
        except Exception as e:
            raise PDFProcessingError(f"Failed to render page {page_number}: {e}") from e
        logger.debug(f"Rendered page {page_number} -> {image_path}")
        return image_path
