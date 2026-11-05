"""Sliding-window translation helpers."""

import re
from pathlib import Path
from typing import Iterable, List, Optional

from ..utils.errors import PromptLoadingError
from ..utils.pages import Page

_PLACEHOLDER_RE = re.compile(r"\{(text|context_section|source_language)\}")


def render_prompt(template: str, text: str, context: Optional[str], source_language: str) -> str:
    """Fill the translation placeholders in a single pass.

    Only ``{text}``, ``{context_section}`` and ``{source_language}`` are
    substituted; any other braces in the template are left as written.
    """
    values = {
        "text": text,
        "context_section": context or "",
        "source_language": source_language,
    }
    return _PLACEHOLDER_RE.sub(lambda m: values[m.group(1)], template)


def check_translation_template(template: str) -> None:
    """Reject a template that would never send the page text to the model."""
    if "{text}" not in template:
        raise PromptLoadingError("The translation prompt must contain the {text} placeholder")


def previous_page_context(pages: List[Page], index: int, translated: dict) -> Optional[str]:
    """The finished translation of the source page before ``pages[index]``.

    Returns None for the first page or when the previous page has no
    successful translation: that clears the sliding window.
    """
    if index == 0:
        return None
    previous = translated.get(pages[index - 1].number)
    if previous is None or previous.is_error or not previous.text:
        return None
    return previous.text


def default_output_path(markdown_file: Path, target_language: str, known_languages: Iterable[str]) -> Path:
    """``book.md`` -> ``book_russian.md`` next to the input.

    A known language suffix is replaced, so translating ``book_english.md``
    into Russian also gives ``book_russian.md``.
    """
    stem = markdown_file.stem
    for language in known_languages:
        suffix = f"_{language}"
        if stem.endswith(suffix) and len(stem) > len(suffix):
            stem = stem[: -len(suffix)]
            break
    return markdown_file.with_name(f"{stem}_{target_language}.md")
