"""--overwrite replaces finished work on purpose: the whole file, or only the pages of --page-range."""

from click.testing import CliRunner

from basira_vision.cli import main
from basira_vision.utils.errors import AuthenticationError, UnfinishedReplyError
from basira_vision.utils.pages import read_page_document

PROMPT = "CTX[{context_section}] SRC[{text}]"


def run(*args):
    return CliRunner().invoke(main, [str(a) for a in args], catch_exceptions=False)


def ocr_output(tmp_path):
    return tmp_path / "workspace" / "book" / "book.md"


def pages_of(path):
    meta, pages = read_page_document(path.read_text())
    return meta, {p.number: (p.text, p.error) for p in pages}


CHECHEN = ["--source-language", "chechen", "--strips", "3", "--dpi", "150", "--thinking-level", "low"]


def edited_book(provider, make_pdf, tmp_path, pages=3, *settings):
    """A finished book whose pages were all edited by hand."""
    pdf = make_pdf(pages)
    assert run("ocr", pdf, *settings).exit_code == 0
    output = ocr_output(tmp_path)
    text = output.read_text()
    for n in range(1, pages + 1):
        text = text.replace(f"text of page {n}", f"edited {n}")
    output.write_text(text)
    provider.ocr_calls.clear()
    return pdf, output


def test_overwrite_without_a_range_starts_over(provider, make_pdf, tmp_path):
    pdf, output = edited_book(provider, make_pdf, tmp_path)
    result = run("ocr", pdf, "--overwrite")
    assert result.exit_code == 0, result.output
    assert provider.ocr_calls == [1, 2, 3]
    assert "edited" not in output.read_text()
    assert "starting over: the previous output was deleted" in result.stderr


def test_overwrite_without_a_range_may_change_the_settings(provider, make_pdf, tmp_path):
    pdf, output = edited_book(provider, make_pdf, tmp_path, 2, *CHECHEN)
    result = run("ocr", pdf, "--overwrite", "--source-language", "english", "--strips", "0")
    assert result.exit_code == 0, result.output
    meta, _ = pages_of(output)
    assert (meta["source_language"], meta["strips"]) == ("english", 0)


def test_force_restart_is_the_old_name_of_overwrite(provider, make_pdf, tmp_path):
    pdf, output = edited_book(provider, make_pdf, tmp_path)
    assert run("ocr", pdf, "--force-restart").exit_code == 0
    assert "edited" not in output.read_text()


def test_overwrite_with_a_range_replaces_only_those_pages(provider, make_pdf, tmp_path):
    pdf, output = edited_book(provider, make_pdf, tmp_path, 4)
    result = run("ocr", pdf, "--overwrite", "--page-range", "2-3")
    assert result.exit_code == 0, result.output
    assert provider.ocr_calls == [2, 3]
    _, pages = pages_of(output)
    assert pages == {1: ("edited 1", None), 2: ("text of page 2", None), 3: ("text of page 3", None),
                     4: ("edited 4", None)}
    assert "replacing pages 2–3, the other pages stay" in result.stderr
    assert "deleted" not in result.stderr


def test_overwrite_with_a_range_continues_with_the_files_settings(provider, make_pdf, tmp_path):
    pdf, output = edited_book(provider, make_pdf, tmp_path, 3, *CHECHEN, "--model", "model-a")
    provider.ocr_requests.clear()
    result = run("ocr", pdf, "--overwrite", "--page-range", "2")
    assert result.exit_code == 0, result.output
    [(page, prompt, strips, media_resolution, thinking_level)] = provider.ocr_requests
    assert page == 2 and strips == 3 and thinking_level == "low"
    assert "CHECHEN-SPECIFIC RULES" in prompt
    assert provider.models[-1] == "model-a"


def test_overwrite_with_a_range_never_mixes_settings(provider, make_pdf, tmp_path):
    pdf, output = edited_book(provider, make_pdf, tmp_path, 3, *CHECHEN)
    before = output.read_text()
    result = run("ocr", pdf, "--overwrite", "--page-range", "2", "--strips", "2")
    assert result.exit_code == 1
    assert "was made with other settings (--strips is 2, the file has 3)" in result.stderr
    # continue: the same pages with the file's settings; restart: the whole book, no range
    assert "basira ocr book.pdf --page-range 2 --overwrite" in result.stderr
    assert "basira ocr book.pdf --strips 2 --overwrite" in result.stderr
    assert provider.ocr_calls == []
    assert output.read_text() == before


def test_overwrite_with_a_range_that_covers_the_book_is_a_restart(provider, make_pdf, tmp_path):
    pdf, output = edited_book(provider, make_pdf, tmp_path, 3, *CHECHEN)
    result = run("ocr", pdf, "--overwrite", "--page-range", "1-3", "--strips", "0")
    assert result.exit_code == 0, result.output
    assert pages_of(output)[0]["strips"] == 0
    assert "starting over" in result.stderr


def test_a_failed_replacement_is_recorded_and_the_rest_stays(provider, make_pdf, tmp_path):
    pdf, output = edited_book(provider, make_pdf, tmp_path, 3)
    provider.fail_pages = {2}
    result = run("ocr", pdf, "--overwrite", "--page-range", "2-3")
    assert result.exit_code == 3
    meta, pages = pages_of(output)
    assert pages[1] == ("edited 1", None)
    assert pages[2] == ("", "boom on page 2")
    assert pages[3] == ("text of page 3", None)
    assert meta["processing_status"] == "Incomplete"

    provider.fail_pages = set()
    provider.ocr_calls.clear()
    assert run("ocr", pdf, "--resume").exit_code == 0
    assert provider.ocr_calls == [2]


def test_overwrite_with_a_range_but_no_file_just_runs(provider, make_pdf, tmp_path):
    result = run("ocr", make_pdf(4), "--overwrite", "--page-range", "2-3")
    assert result.exit_code == 0, result.output
    assert provider.ocr_calls == [2, 3]
    assert sorted(pages_of(ocr_output(tmp_path))[1]) == [2, 3]
    assert "replacing" not in result.stderr and "starting over" not in result.stderr


def test_refusal_offers_overwrite_and_says_what_it_replaces(provider, make_pdf, tmp_path):
    pdf, output = edited_book(provider, make_pdf, tmp_path, 4)
    result = run("ocr", pdf, "--page-range", "2-3")
    assert result.exit_code == 1
    assert "basira ocr book.pdf --page-range 2-3 --overwrite" in result.stderr
    assert "replaces pages 2–3, keeps the other pages" in result.stderr
    assert "--force-restart" not in result.stderr

    whole = run("ocr", pdf)
    assert "basira ocr book.pdf --overwrite" in whole.stderr
    assert "deletes it and starts over" in whole.stderr


def test_overwrite_and_resume_exclude_each_other(provider, make_pdf):
    result = run("ocr", make_pdf(2), "--resume", "--overwrite")
    assert result.exit_code == 2
    assert "--resume and --overwrite cannot be used together" in result.stderr


def test_translate_overwrite_starts_over(provider, make_pdf, tmp_path):
    assert run("ocr", make_pdf(2)).exit_code == 0
    source = ocr_output(tmp_path)
    translate = ["translate", source, "--target-language", "russian", "--custom-prompt", PROMPT]
    assert run(*translate).exit_code == 0
    translation = source.with_name("book_russian.md")
    translation.write_text(translation.read_text().replace("tr(text of page 1)", "my manual fix"))

    refused = run(*translate)
    assert refused.exit_code == 1
    assert "--overwrite" in refused.stderr and "--force-restart" not in refused.stderr
    assert "my manual fix" in translation.read_text()

    result = run(*translate, "--overwrite")
    assert result.exit_code == 0, result.output
    assert "my manual fix" not in translation.read_text()
    assert "starting over: the previous translation was deleted" in result.stderr


def test_an_interrupted_overwrite_offers_overwrite_for_the_pages_not_reached(provider, make_pdf, tmp_path):
    pdf, output = edited_book(provider, make_pdf, tmp_path, 5)
    provider.raise_on_page = {3: AuthenticationError("the endpoint rejected the API key (HTTP 401)")}
    result = run("ocr", pdf, "--overwrite", "--page-range", "2-5")
    assert result.exit_code == 1
    _, pages = pages_of(output)
    assert pages[2] == ("text of page 2", None)
    assert pages[3] == ("edited 3", None) and pages[4] == ("edited 4", None)
    # --resume would skip the pages that still hold the old text
    assert "basira ocr book.pdf --page-range 3-5 --overwrite" in result.stderr
    assert "--resume" not in result.stderr

    provider.raise_on_page = {}
    provider.ocr_calls.clear()
    assert run("ocr", pdf, "--page-range", "3-5", "--overwrite").exit_code == 0
    assert provider.ocr_calls == [3, 4, 5]


def test_overwrite_with_an_inline_prompt_hint_says_overwrite(provider, make_pdf, tmp_path):
    pdf, output = edited_book(provider, make_pdf, tmp_path, 3, "--custom-prompt", "Read carefully.")
    result = run("ocr", pdf, "--page-range", "2", "--overwrite")
    assert result.exit_code == 1
    assert "pass the same --custom-prompt text again with --overwrite" in result.stderr
    assert "--resume" not in result.stderr


def test_overwrite_with_a_range_longer_than_the_book_is_a_restart(provider, make_pdf, tmp_path):
    pdf, output = edited_book(provider, make_pdf, tmp_path, 3, *CHECHEN)
    result = run("ocr", pdf, "--overwrite", "--page-range", "1-10", "--strips", "0")
    assert result.exit_code == 0, result.output
    assert provider.ocr_calls == [1, 2, 3] and pages_of(output)[0]["strips"] == 0


def test_translate_overwrite_may_change_the_settings(provider, make_pdf, tmp_path):
    assert run("ocr", make_pdf(2)).exit_code == 0
    source = ocr_output(tmp_path)
    translate = ["translate", source, "--target-language", "russian", "--custom-prompt", PROMPT]
    assert run(*translate, "--model", "model-a").exit_code == 0
    assert run(*translate, "--model", "model-b", "--overwrite").exit_code == 0
    assert read_page_document(source.with_name("book_russian.md").read_text())[0]["ai_model"] == "model-b"
    assert provider.models[-1] == "model-b"


def test_translate_accepts_the_old_name_and_still_excludes_resume(provider, make_pdf, tmp_path):
    assert run("ocr", make_pdf(2)).exit_code == 0
    source = ocr_output(tmp_path)
    translate = ["translate", source, "--target-language", "russian", "--custom-prompt", PROMPT]
    assert run(*translate).exit_code == 0
    assert run(*translate, "--force-restart").exit_code == 0
    assert run(*translate, "--resume", "--overwrite").exit_code == 2
    assert run(*translate, "--resume", "--force-restart").exit_code == 2


def test_ocr_resume_and_the_old_name_exclude_each_other(provider, make_pdf):
    assert run("ocr", make_pdf(2), "--resume", "--force-restart").exit_code == 2


def test_the_old_name_is_hidden_from_help():
    for command in ("ocr", "translate"):
        help_text = run(command, "--help").output
        assert "--overwrite" in help_text and "--force-restart" not in help_text


def test_overwrite_without_an_output_just_runs(provider, make_pdf, tmp_path):
    result = run("ocr", make_pdf(2), "--overwrite")
    assert result.exit_code == 0, result.output
    assert provider.ocr_calls == [1, 2]
    assert "starting over" not in result.stderr


def test_blocked_pages_are_recorded_with_the_reason_and_retried(provider, make_pdf, tmp_path):
    pdf = make_pdf(3)
    provider.raise_on_page = {2: UnfinishedReplyError("reply stopped: RECITATION")}
    assert run("ocr", pdf).exit_code == 3
    assert "<!-- Page 2 - Error: reply stopped: RECITATION -->" in ocr_output(tmp_path).read_text()

    provider.raise_on_page = {}
    provider.ocr_calls.clear()
    assert run("ocr", pdf, "--resume").exit_code == 0
    assert provider.ocr_calls == [2]


def test_blocked_pages_do_not_stop_the_run_like_a_dead_endpoint(provider, make_pdf, tmp_path):
    provider.raise_on_page = {n: UnfinishedReplyError("reply stopped: RECITATION") for n in range(2, 9)}
    result = run("ocr", make_pdf(10))
    assert result.exit_code == 3
    assert provider.ocr_calls == list(range(1, 11))
    assert "in a row" not in result.stderr
