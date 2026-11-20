"""--resume continues the same job: the settings recorded in the file, never a silent mix."""

from click.testing import CliRunner

from basira_vision.cli import main
from basira_vision.utils.pages import read_page_document


def run(*args):
    return CliRunner().invoke(main, [str(a) for a in args], catch_exceptions=False)


def ocr_output(tmp_path):
    return tmp_path / "workspace" / "book" / "book.md"


def meta_of(path):
    return read_page_document(path.read_text())[0]


CHECHEN = ["--source-language", "chechen", "--strips", "3", "--dpi", "150", "--thinking-level", "low"]


def test_ocr_records_the_settings_that_shape_the_text(provider, make_pdf, tmp_path):
    assert run("ocr", make_pdf(1), *CHECHEN, "--model", "model-a").exit_code == 0
    meta = meta_of(ocr_output(tmp_path))
    assert meta["source_language"] == "chechen"
    assert meta["ai_model"] == "model-a"
    assert (meta["dpi"], meta["strips"], meta["thinking_level"]) == (150, 3, "low")
    assert meta["media_resolution"] is None and meta["custom_prompt"] is None and meta["prompts_dir"] is None
    assert meta["image_format"] == "PNG"


def test_resume_continues_with_the_files_settings(provider, make_pdf, tmp_path):
    pdf = make_pdf(3)
    provider.fail_pages = {2}
    assert run("ocr", pdf, *CHECHEN, "--model", "model-a").exit_code == 3

    provider.fail_pages = set()
    provider.ocr_requests.clear()
    result = run("ocr", pdf, "--resume")
    assert result.exit_code == 0, result.output
    [(page, prompt, strips, media_resolution, thinking_level)] = provider.ocr_requests
    assert page == 2
    assert "CHECHEN-SPECIFIC RULES" in prompt
    assert (strips, media_resolution, thinking_level) == (3, None, "low")
    assert provider.models[-1] == "model-a"
    assert "150 dpi" in result.stderr and "page + 3 strips" in result.stderr


def test_resume_stops_when_a_typed_setting_differs_from_the_file(provider, make_pdf, tmp_path):
    pdf = make_pdf(2)
    provider.fail_pages = {2}
    run("ocr", pdf, *CHECHEN)
    before = ocr_output(tmp_path).read_text()

    provider.ocr_calls.clear()
    result = run("ocr", pdf, "--resume", "--source-language", "english", "--strips", "0")
    assert result.exit_code == 1
    assert "was made with other settings" in result.stderr
    assert "--source-language is english, the file has chechen" in result.stderr
    assert "--strips is 0, the file has 3" in result.stderr
    assert "basira ocr book.pdf --resume" in result.stderr
    assert "--overwrite" in result.stderr
    assert provider.ocr_calls == []
    assert ocr_output(tmp_path).read_text() == before


def test_resume_accepts_the_same_settings_typed_again(provider, make_pdf):
    pdf = make_pdf(2)
    provider.fail_pages = {2}
    run("ocr", pdf, *CHECHEN)
    provider.fail_pages = set()
    assert run("ocr", pdf, *CHECHEN, "--resume").exit_code == 0


def test_resume_prefers_the_files_model_over_the_environment(provider, make_pdf, monkeypatch):
    pdf = make_pdf(2)
    provider.fail_pages = {2}
    run("ocr", pdf, "--model", "model-a")
    provider.fail_pages = set()

    monkeypatch.setenv("GEMINI_MODEL", "model-b")
    assert run("ocr", pdf, "--resume").exit_code == 0
    assert provider.models[-1] == "model-a"

    result = run("ocr", pdf, "--resume", "--model", "model-b")
    assert result.exit_code == 1
    assert "--model is model-b, the file has model-a" in result.stderr


def test_resume_never_starts_a_new_file(provider, make_pdf, tmp_path):
    result = run("ocr", make_pdf(2), "--resume")
    assert result.exit_code == 1
    assert "nothing to resume" in result.stderr
    assert provider.ocr_calls == []
    assert not ocr_output(tmp_path).exists()


def test_resume_reuses_a_prompt_file(provider, make_pdf, tmp_path):
    prompt_file = tmp_path / "my_prompt.txt"
    prompt_file.write_text("Read this page exactly.")
    pdf = make_pdf(2)
    provider.fail_pages = {2}
    run("ocr", pdf, "--custom-prompt", prompt_file)
    assert meta_of(ocr_output(tmp_path))["custom_prompt"] == "../../my_prompt.txt"

    provider.fail_pages = set()
    provider.ocr_requests.clear()
    assert run("ocr", pdf, "--resume").exit_code == 0
    assert provider.ocr_requests[0][1] == "Read this page exactly."


def test_resume_needs_an_inline_prompt_again(provider, make_pdf):
    pdf = make_pdf(2)
    provider.fail_pages = {2}
    run("ocr", pdf, "--custom-prompt", "Read this page.")
    provider.fail_pages = set()

    result = run("ocr", pdf, "--resume")
    assert result.exit_code == 1
    assert "--custom-prompt given as text" in result.stderr

    result = run("ocr", pdf, "--resume", "--custom-prompt", "Something else.")
    assert result.exit_code == 1
    assert "was made with other settings" in result.stderr

    provider.ocr_requests.clear()
    assert run("ocr", pdf, "--resume", "--custom-prompt", "Read this page.").exit_code == 0
    assert provider.ocr_requests[0][1] == "Read this page."


PROMPT = "CTX[{context_section}] SRC[{text}]"
PAGES = "<!-- Page 1 -->\none\n\n---\n\n<!-- Page 2 -->\ntwo\n"


def write_source(tmp_path):
    source = tmp_path / "book.md"
    source.write_text(f'---\ntitle: "Book"\nsource_language: "chechen"\n---\n\n{PAGES}')
    return source


def test_translate_resume_names_the_existing_translation(provider, tmp_path):
    source = write_source(tmp_path)
    provider.fail_texts = {"two"}
    assert run("translate", source, "--target-language", "russian", "--custom-prompt", PROMPT).exit_code == 3
    meta = meta_of(tmp_path / "book_russian.md")
    assert meta["target_language"] == "russian" and meta["custom_prompt"].startswith("inline:")

    provider.fail_texts = set()
    provider.prompts.clear()
    result = run("translate", source, "--custom-prompt", PROMPT, "--resume")
    assert result.exit_code == 1
    assert "nothing to resume" in result.stderr and "book_russian.md" in result.stderr
    assert provider.prompts == []
    assert not (tmp_path / "book_english.md").exists()

    result = run("translate", source, "--target-language", "russian", "--custom-prompt", PROMPT, "--resume")
    assert result.exit_code == 0, result.output
    assert provider.prompts == ["CTX[tr(one)] SRC[two]"]


def test_translate_resume_stops_on_another_model(provider, tmp_path):
    source = write_source(tmp_path)
    provider.fail_texts = {"two"}
    run("translate", source, "--target-language", "russian", "--custom-prompt", PROMPT, "--model", "model-a")
    result = run("translate", source, "--target-language", "russian", "--custom-prompt", PROMPT,
                 "--model", "model-b", "--resume")
    assert result.exit_code == 1
    assert "--model is model-b, the file has model-a" in result.stderr


# ---------------------------------------------------------------------------- review findings


def test_translate_with_an_output_file_resumes_in_the_files_language(provider, tmp_path):
    source = write_source(tmp_path)
    out = tmp_path / "custom_out.md"
    provider.fail_texts = {"two"}
    run("translate", source, "-o", out, "--target-language", "russian", "--custom-prompt", PROMPT)

    result = run("translate", source, "-o", out, "--custom-prompt", PROMPT, "--resume")
    assert result.exit_code == 1  # the only page tried fails again
    assert "→ russian" in result.stderr
    assert "--target-language russian" in result.stderr and "--target-language english" not in result.stderr


def test_a_missing_prompt_file_is_reported_with_a_hint_that_works(provider, make_pdf, tmp_path):
    prompt_file = tmp_path / "p1.txt"
    prompt_file.write_text("Read it.")
    pdf = make_pdf(2)
    provider.fail_pages = {2}
    run("ocr", pdf, "--custom-prompt", prompt_file)
    prompt_file.unlink()

    result = run("ocr", pdf, "--resume")
    assert result.exit_code == 1
    assert "the prompt file of" in result.stderr and "restore it at that path" in result.stderr
    assert "pass --custom-prompt with its new path" not in result.stderr


def test_a_missing_prompts_dir_stops_instead_of_falling_back(provider, make_pdf, tmp_path):
    prompts = tmp_path / "custom_p" / "ocr"
    prompts.mkdir(parents=True)
    (prompts / "english.txt").write_text("My own English prompt.")
    pdf = make_pdf(2)
    provider.fail_pages = {2}
    run("ocr", pdf, "--prompts-dir", tmp_path / "custom_p")
    (prompts / "english.txt").unlink()
    prompts.rmdir()
    prompts.parent.rmdir()

    provider.ocr_calls.clear()
    result = run("ocr", pdf, "--resume")
    assert result.exit_code == 1
    assert "the prompt directory of" in result.stderr
    assert provider.ocr_calls == []


def test_the_continue_hint_keeps_an_inline_prompt(provider, make_pdf):
    pdf = make_pdf(2)
    provider.fail_pages = {2}
    run("ocr", pdf, "--custom-prompt", "Read carefully.", "--strips", "3")
    result = run("ocr", pdf, "--resume", "--custom-prompt", "Read carefully.", "--strips", "0")
    assert result.exit_code == 1
    assert "basira ocr book.pdf --custom-prompt 'Read carefully.' --resume" in result.stderr


def test_hand_edited_settings_are_read_like_typed_ones(provider, make_pdf, tmp_path):
    pdf = make_pdf(2)
    provider.fail_pages = {2}
    run("ocr", pdf, "--dpi", "150")
    output = ocr_output(tmp_path)
    output.write_text(output.read_text().replace("dpi: 150", 'dpi: "150"')
                      .replace('image_format: "PNG"', "image_format: png"))
    provider.fail_pages = set()
    assert run("ocr", pdf, "--resume", "--dpi", "150", "--image-format", "PNG").exit_code == 0


def test_an_invalid_recorded_setting_stops_with_a_message(provider, make_pdf, tmp_path):
    pdf = make_pdf(2)
    provider.fail_pages = {2}
    run("ocr", pdf)
    output = ocr_output(tmp_path)
    output.write_text(output.read_text().replace('source_language: "english"', "source_language: no"))
    result = run("ocr", pdf, "--resume")
    assert result.exit_code == 1
    assert "invalid source_language in its frontmatter: False" in result.stderr


def test_recorded_paths_avoid_the_users_name(monkeypatch, tmp_path):
    from pathlib import Path

    from basira_vision.cli import _recorded_path

    home = tmp_path / "home"
    (home / "prompts").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    elsewhere = Path("/opt/basira-test/book/book.md")  # shares only the root with the home folder
    assert _recorded_path(home / "prompts", elsewhere) == "~/prompts"
    assert _recorded_path(Path("/usr/share/prompts"), elsewhere) == "/usr/share/prompts"
    near = home / "work" / "book" / "book.md"
    assert _recorded_path(home / "prompts", near) == "../../prompts"


def test_translate_never_overwrites_its_source(provider, tmp_path):
    source = tmp_path / "book_english.md"
    source.write_text(f'---\ntitle: "Book"\n---\n\n{PAGES}')
    result = run("translate", source, "--custom-prompt", PROMPT)
    assert result.exit_code == 1
    assert "would overwrite its source" in result.stderr
    assert provider.prompts == []


def test_nothing_to_resume_lists_translations_and_skips_folders(provider, tmp_path):
    source = write_source(tmp_path)
    provider.fail_texts = {"two"}
    run("translate", source, "--target-language", "russian", "--custom-prompt", PROMPT)
    (tmp_path / "book_dir.md").mkdir()
    result = run("translate", source, "--target-language", "pt_br", "--custom-prompt", PROMPT, "--resume")
    assert result.exit_code == 1
    assert "book_russian.md" in result.stderr


def test_a_long_inline_prompt_is_not_taken_for_a_file_name(provider, make_pdf, tmp_path):
    long_prompt = "Read every letter. " * 40
    assert run("ocr", make_pdf(1), "--custom-prompt", long_prompt).exit_code == 0
    assert meta_of(ocr_output(tmp_path))["custom_prompt"].startswith("inline:")
