"""Shared constants."""

from enum import Enum


class SeparatorType(Enum):
    """How pages are separated in a page file."""

    OCR = "ocr"  # markdown thematic break
    TYPST = "typst"  # raw Typst page break, used for translations


DEFAULT_TARGET_LANGUAGE = "english"

# Language names used by the prompt templates mapped to ISO 639-1 codes,
# used to pick the document language for typesetting.
LANGUAGE_CODES = {
    "arabic": "ar",
    "chechen": "ce",
    "english": "en",
    "french": "fr",
    "german": "de",
    "russian": "ru",
}
