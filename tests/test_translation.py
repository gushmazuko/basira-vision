from pathlib import Path

import pytest

from basira_vision.processing.translation import (
    check_translation_template,
    default_output_path,
    previous_page_context,
    render_prompt,
)
from basira_vision.utils.errors import PromptLoadingError
from basira_vision.utils.pages import Page


def test_render_prompt_leaves_other_braces_alone():
    template = "Keep {braces} and {{these}}.\nCTX: {context_section}\nFROM {source_language}: {text}"
    rendered = render_prompt(template, text="a {text} b", context=None, source_language="French")
    assert rendered == "Keep {braces} and {{these}}.\nCTX: \nFROM French: a {text} b"


def test_template_without_text_placeholder_is_rejected():
    with pytest.raises(PromptLoadingError):
        check_translation_template("Translate this: {context_section}")
    check_translation_template("{text}")


def test_context_is_the_previous_finished_page():
    pages = [Page(1, "a"), Page(2, "b"), Page(4, "d")]
    translated = {1: Page(1, "A"), 2: Page(2, "", error="HTTP 500")}

    assert previous_page_context(pages, 0, translated) is None
    assert previous_page_context(pages, 1, translated) == "A"
    assert previous_page_context(pages, 2, translated) is None  # window cleared after an error


def test_default_output_path_replaces_a_language_suffix():
    known = ["english", "russian"]
    assert default_output_path(Path("/x/book.md"), "russian", known) == Path("/x/book_russian.md")
    assert default_output_path(Path("/x/book_english.md"), "russian", known) == Path("/x/book_russian.md")
    assert default_output_path(Path("/x/english.md"), "russian", known) == Path("/x/english_russian.md")
