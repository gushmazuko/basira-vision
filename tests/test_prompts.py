import pytest

from basira_vision.processing.prompt_loader import PromptLoader, read_custom_prompt
from basira_vision.processing.translation import check_translation_template
from basira_vision.utils.errors import PromptLoadingError

loader = PromptLoader()


def test_builtin_languages():
    languages = ["arabic", "chechen", "english", "french", "german", "russian"]
    assert loader.available("ocr") == languages
    assert loader.available("translate") == languages


@pytest.mark.parametrize("language", PromptLoader().available("translate"))
def test_translate_templates_have_all_placeholders(language):
    template = loader.load("translate", language)
    check_translation_template(template)
    assert "{context_section}" in template
    assert "{source_language}" in template


@pytest.mark.parametrize("language", PromptLoader().available("ocr"))
def test_ocr_templates_have_no_placeholders(language):
    assert "{" not in loader.load("ocr", language)


def test_unknown_language_lists_available_ones():
    with pytest.raises(PromptLoadingError, match="english"):
        loader.load("ocr", "klingon")


def test_user_directory_adds_and_overrides(tmp_path):
    (tmp_path / "ocr").mkdir()
    (tmp_path / "ocr" / "klingon.txt").write_text("read Klingon")
    (tmp_path / "ocr" / "english.txt").write_text("my english")

    user_loader = PromptLoader(tmp_path)
    assert "klingon" in user_loader.available("ocr")
    assert user_loader.load("ocr", "Klingon") == "read Klingon"
    assert user_loader.load("ocr", "english") == "my english"
    assert user_loader.load("ocr", "french") == loader.load("ocr", "french")


def test_custom_prompt_from_text_or_file(tmp_path):
    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text("  from a file  \n")
    assert read_custom_prompt(str(prompt_file)) == "from a file"
    assert read_custom_prompt("inline prompt") == "inline prompt"
