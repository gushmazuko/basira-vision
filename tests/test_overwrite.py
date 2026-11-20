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


def test_overwrite_with_a_range_reads_those_pages_with_the_typed_settings(provider, make_pdf, tmp_path):
    pdf, output = edited_book(provider, make_pdf, tmp_path, 3, *CHECHEN)
    frontmatter = output.read_text().split("---")[1]
    provider.ocr_requests.clear()
    result = run("ocr", pdf, "--overwrite", "--page-range", "2", "--strips", "2")
    assert result.exit_code == 0, result.output
    [(page, prompt, strips, media_resolution, thinking_level)] = provider.ocr_requests
    # typed: --strips; everything else still comes from the file
    assert (page, strips, thinking_level) == (2, 2, "low")
    assert "CHECHEN-SPECIFIC RULES" in prompt
    assert "page 2 is read with other settings than the file (--strips is 2, the file has 3)" in result.stderr
    # the frontmatter keeps describing the file, the other pages stay
    after = output.read_text().split("---")[1]
    assert after.replace('"Completed"', "") == frontmatter.replace('"Completed"', "")
    meta, pages = pages_of(output)
    assert meta["strips"] == 3
    assert pages[1] == ("edited 1", None) and pages[2] == ("text of page 2", None) and pages[3] == ("edited 3", None)


def test_overwrite_with_a_range_and_the_files_settings_typed_again_does_not_warn(provider, make_pdf, tmp_path):
    pdf, _ = edited_book(provider, make_pdf, tmp_path, 3, *CHECHEN)
    result = run("ocr", pdf, "--overwrite", "--page-range", "2", "--strips", "3")
    assert result.exit_code == 0, result.output
    assert "other settings" not in result.stderr


def test_overwrite_with_a_range_may_use_another_prompt(provider, make_pdf, tmp_path):
    pdf = make_pdf(3)
    provider.fail_pages = {3}
    assert run("ocr", pdf, *CHECHEN).exit_code == 3
    output = ocr_output(tmp_path)
    other = tmp_path / "other.txt"
    other.write_text("READ THIS PAGE ANOTHER WAY")
    provider.ocr_requests.clear()
    result = run("ocr", pdf, "--overwrite", "--page-range", "2", "--custom-prompt", other)
    assert result.exit_code == 0, result.output
    [(page, prompt, strips, _, _)] = provider.ocr_requests
    assert page == 2 and prompt.startswith("READ THIS PAGE ANOTHER WAY") and strips == 3
    assert "page 2 is read with other settings than the file (--custom-prompt is" in result.stderr
    assert pages_of(output)[0]["custom_prompt"] is None

    # a later --resume reads the failed page with the file's own prompt, not with the override
    provider.fail_pages = set()
    provider.ocr_requests.clear()
    assert run("ocr", pdf, "--resume").exit_code == 0
    [(page, prompt, strips, _, thinking_level)] = provider.ocr_requests
    assert page == 3 and "CHECHEN-SPECIFIC RULES" in prompt and (strips, thinking_level) == (3, "low")


def test_several_pages_and_several_typed_settings_are_named_in_the_warning(provider, make_pdf, tmp_path):
    pdf, output = edited_book(provider, make_pdf, tmp_path, 4, *CHECHEN, "--model", "model-a")
    provider.ocr_requests.clear()
    result = run("ocr", pdf, "--overwrite", "--page-range", "2-3", "--model", "model-b",
                 "--source-language", "english", "--dpi", "200")
    assert result.exit_code == 0, result.output
    assert "pages 2–3 are read with other settings than the file (--source-language is english, the file has " \
           "chechen; --model is model-b, the file has model-a; --dpi is 200, the file has 150)" in result.stderr
    assert provider.models[-1] == "model-b"
    assert all("CHECHEN-SPECIFIC RULES" not in prompt and strips == 3 for _, prompt, strips, _, _ in
               provider.ocr_requests)
    meta, _ = pages_of(output)
    assert (meta["ai_model"], meta["source_language"], meta["dpi"]) == ("model-a", "chechen", 150)


def test_a_typed_setting_equal_to_the_default_stays_in_the_retry_hint(provider, make_pdf, tmp_path):
    pdf, _ = edited_book(provider, make_pdf, tmp_path, 4, *CHECHEN)
    provider.fail_pages = {3}
    args = ["--strips", "0", "--source-language", "english", "--dpi", "300"]
    result = run("ocr", pdf, "--overwrite", "--page-range", "2-3", *args)
    assert result.exit_code == 3
    # without them the retry would read page 3 with the file's --strips 3, chechen and 150 dpi
    assert "basira ocr book.pdf --source-language english --dpi 300 --strips 0 --thinking-level low " \
           "--page-range 3 --overwrite" in result.stderr

    # the hint does what its note says: only the failed page, with the same settings
    provider.fail_pages = set()
    provider.ocr_requests.clear()
    retry = run("ocr", pdf, "--source-language", "english", "--dpi", "300", "--strips", "0",
                "--thinking-level", "low", "--page-range", "3", "--overwrite")
    assert retry.exit_code == 0, retry.output
    [(page, prompt, strips, _, thinking_level)] = provider.ocr_requests
    assert (page, strips, thinking_level) == (3, 0, "low") and "CHECHEN-SPECIFIC RULES" not in prompt


def test_failed_pages_with_a_gap_get_a_text_hint(provider, make_pdf, tmp_path):
    pdf, _ = edited_book(provider, make_pdf, tmp_path, 5, *CHECHEN)
    provider.fail_pages = {2, 4}
    result = run("ocr", pdf, "--overwrite", "--page-range", "2-4", "--strips", "2")
    assert result.exit_code == 3
    assert "pages 2, 4 failed" in result.stderr
    assert "read them again with --page-range … --overwrite and the same options" in result.stderr
    assert "--resume" not in result.stderr


def test_a_stopped_overwrite_with_typed_settings_continues_with_them(provider, make_pdf, tmp_path):
    pdf, output = edited_book(provider, make_pdf, tmp_path, 5, *CHECHEN)
    provider.raise_on_page = {3: AuthenticationError("the endpoint rejected the API key (HTTP 401)")}
    result = run("ocr", pdf, "--overwrite", "--page-range", "2-4", "--strips", "0")
    assert result.exit_code == 1
    assert "--strips 0 --thinking-level low --page-range 3-4 --overwrite" in result.stderr
    assert "--resume" not in result.stderr
    assert pages_of(output)[1][3] == ("edited 3", None)


def test_five_failures_in_a_row_with_typed_settings_say_to_fix_the_cause(provider, make_pdf, tmp_path):
    pdf, _ = edited_book(provider, make_pdf, tmp_path, 8, *CHECHEN)
    provider.fail_pages = set(range(2, 7))
    result = run("ocr", pdf, "--overwrite", "--page-range", "2-6", "--strips", "2")
    assert result.exit_code == 1
    assert "--strips 2 --thinking-level low --page-range 2-6 --overwrite" in result.stderr
    assert "once the cause is fixed" in result.stderr


def test_resume_still_stops_on_a_typed_setting_that_differs(provider, make_pdf, tmp_path):
    pdf, output = edited_book(provider, make_pdf, tmp_path, 3, *CHECHEN)
    before = output.read_text()
    result = run("ocr", pdf, "--resume", "--page-range", "2", "--strips", "2")
    assert result.exit_code == 1
    assert "was made with other settings (--strips is 2, the file has 3)" in result.stderr
    assert "basira ocr book.pdf --page-range 2 --resume" in result.stderr
    # restart: the whole book, the file's settings with the typed one on top (not the built-in defaults)
    assert "basira ocr book.pdf --source-language chechen --dpi 150 --strips 2 --thinking-level low --overwrite" \
           in result.stderr
    assert provider.ocr_calls == [] and output.read_text() == before


def test_a_failed_page_read_with_typed_settings_is_retried_with_overwrite(provider, make_pdf, tmp_path):
    pdf, _ = edited_book(provider, make_pdf, tmp_path, 4, *CHECHEN)
    provider.fail_pages = {3}
    result = run("ocr", pdf, "--overwrite", "--page-range", "2-3", "--strips", "2")
    assert result.exit_code == 3
    # --resume would stop on --strips 2, or retry page 3 with the file's settings
    assert "--strips 2 --thinking-level low --page-range 3 --overwrite" in result.stderr
    assert "--resume" not in result.stderr


def test_remaining_pages_are_not_offered_with_the_typed_settings(provider, make_pdf, tmp_path):
    pdf = make_pdf(4)
    assert run("ocr", pdf, *CHECHEN, "--page-range", "1-2").exit_code == 0
    result = run("ocr", pdf, "--overwrite", "--page-range", "2", "--strips", "2")
    assert result.exit_code == 0, result.output
    # a --resume with --strips 2 would stop: the hint names the pages, not a command with the typed settings
    assert "pages 3–4 not processed yet" in result.stderr
    assert "process them with --page-range" in result.stderr
    assert "--page-range 3-4 --resume" not in result.stderr


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


# ---------------------------------------------------------------------------- found by cross review


def test_translate_restart_hint_keeps_the_files_language(provider, make_pdf, tmp_path):
    assert run("ocr", make_pdf(2)).exit_code == 0
    out = tmp_path / "out.md"
    translate = ["translate", ocr_output(tmp_path), "--output", out, "--custom-prompt", PROMPT]
    assert run(*translate, "--target-language", "russian", "--model", "model-a").exit_code == 0
    result = run(*translate, "--model", "model-b", "--resume")
    assert result.exit_code == 1
    # starting over must not silently switch the translation to the default language
    assert "--target-language russian" in result.stderr and "--model model-b --overwrite" in result.stderr
    assert "--target-language english" not in result.stderr


def test_a_frontmatter_that_cannot_be_read_stops_resume_and_page_overwrite(provider, make_pdf, tmp_path):
    pdf = make_pdf(3)
    provider.fail_pages = {2}
    assert run("ocr", pdf, *CHECHEN).exit_code == 3
    output = ocr_output(tmp_path)
    output.write_text(output.read_text().replace('title: ""', "title: [unclosed", 1))
    before = output.read_text()
    provider.fail_pages = set()
    provider.ocr_calls.clear()
    for flags in (["--resume"], ["--page-range", "2", "--overwrite"]):
        result = run("ocr", pdf, *flags)
        assert result.exit_code == 1, result.output
        assert "has a frontmatter that cannot be read" in result.stderr
    # otherwise the run would go on with the built-in defaults instead of the file's settings
    assert provider.ocr_calls == [] and output.read_text() == before


def test_pages_that_failed_in_an_earlier_run_are_not_forgotten(provider, make_pdf, tmp_path):
    pdf = make_pdf(5)
    provider.fail_pages = {2}
    assert run("ocr", pdf, "--page-range", "1-3").exit_code == 3
    provider.fail_pages = set()
    result = run("ocr", pdf, "--page-range", "4-5", "--resume")
    assert result.exit_code == 0, result.output
    assert "page 2 failed in an earlier run" in result.stderr
    assert "retry it with --resume" in result.stderr


def test_a_mistyped_prompts_dir_on_a_new_run_is_not_blamed_on_the_output(provider, make_pdf, tmp_path):
    result = run("ocr", make_pdf(1), "--prompts-dir", tmp_path / "does_not_exist")
    assert result.exit_code == 1
    assert "the prompt directory does not exist" in result.stderr
    assert "--overwrite" not in result.stderr and "is missing" not in result.stderr


def test_inline_prompts_are_named_not_fingerprinted(provider, make_pdf, tmp_path):
    pdf, _ = edited_book(provider, make_pdf, tmp_path, 3, "--custom-prompt", "Read A.")
    result = run("ocr", pdf, "--resume", "--custom-prompt", "Read B.")
    assert result.exit_code == 1
    assert "--custom-prompt is another text than the file's" in result.stderr
    assert "inline:" not in result.stderr

    other = tmp_path / "other.txt"
    other.write_text("READ THIS PAGE ANOTHER WAY")
    result = run("ocr", pdf, "--page-range", "2", "--overwrite", "--custom-prompt", other)
    assert result.exit_code == 0, result.output
    assert "the file has inline text" in result.stderr and "inline:" not in result.stderr


def test_a_typed_language_does_not_claim_to_change_pages_read_with_a_custom_prompt(provider, make_pdf, tmp_path):
    custom = tmp_path / "custom.txt"
    custom.write_text("CUSTOM PROMPT")
    pdf, _ = edited_book(provider, make_pdf, tmp_path, 3, "--custom-prompt", custom)
    provider.ocr_requests.clear()
    result = run("ocr", pdf, "--page-range", "2", "--overwrite", "--source-language", "chechen")
    assert result.exit_code == 0, result.output
    assert provider.ocr_requests[0][1].startswith("CUSTOM PROMPT")
    assert "read with other settings" not in result.stderr
    assert "--source-language has no effect on page 2: it is read with the --custom-prompt" in result.stderr


def test_an_empty_custom_prompt_reads_the_pages_with_the_language_prompt(provider, make_pdf, tmp_path):
    custom = tmp_path / "custom.txt"
    custom.write_text("CUSTOM PROMPT")
    pdf, output = edited_book(provider, make_pdf, tmp_path, 3, "--custom-prompt", custom)
    provider.fail_pages = {2}
    provider.ocr_requests.clear()
    result = run("ocr", pdf, "--page-range", "2", "--overwrite", "--custom-prompt", "")
    assert result.exit_code == 1
    assert not provider.ocr_requests[0][1].startswith("CUSTOM PROMPT")
    # without the empty option the retry would read the page with the file's custom prompt again
    assert "basira ocr book.pdf --custom-prompt '' --page-range 2 --overwrite" in result.stderr


def test_one_remaining_page_is_not_called_them(provider, make_pdf, tmp_path):
    pdf = make_pdf(3)
    assert run("ocr", pdf, *CHECHEN, "--page-range", "1-2").exit_code == 0
    result = run("ocr", pdf, "--overwrite", "--page-range", "2", "--strips", "2")
    assert result.exit_code == 0, result.output
    assert "page 3 not processed yet" in result.stderr
    assert "process it with --page-range … --resume, without the options typed for this run" in result.stderr
