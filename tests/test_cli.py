import json
import os
import signal

from click.testing import CliRunner

from basira_vision.cli import main
from basira_vision.utils.errors import AuthenticationError, EndpointUnreachableError
from basira_vision.utils.pages import read_page_document

PROMPT = "CTX[{context_section}] SRC[{text}]"


def run(*args):
    return CliRunner().invoke(main, [str(a) for a in args], catch_exceptions=False)


def ocr_output(tmp_path):
    return tmp_path / "workspace" / "book" / "book.md"


def pages_of(path):
    meta, pages = read_page_document(path.read_text())
    return meta, {p.number: (p.text, p.error) for p in pages}


# ---------------------------------------------------------------------------- ocr


def test_ocr_writes_every_page(provider, make_pdf, tmp_path):
    result = run("ocr", make_pdf(3))
    assert result.exit_code == 0, result.output

    meta, pages = pages_of(ocr_output(tmp_path))
    assert pages == {n: (f"text of page {n}", None) for n in (1, 2, 3)}
    assert meta["processing_status"] == "Completed"
    assert meta["source_language"] == "english"
    assert sorted(p.name for p in (ocr_output(tmp_path).parent / "images").iterdir()) == [
        "page_001.png", "page_002.png", "page_003.png"
    ]


def test_result_path_goes_to_stdout_and_progress_to_stderr(provider, make_pdf):
    result = run("ocr", make_pdf(2))
    assert result.stdout == "workspace/book/book.md\n"
    assert "ocr ok      page=2 progress=2/2" in result.stderr
    assert "basira translate workspace/book/book.md --target-language <language>" in result.stderr


def test_page_ranges_keep_real_page_numbers_and_resume_in_chunks(provider, make_pdf, tmp_path):
    pdf = make_pdf(5)

    result = run("ocr", pdf, "--page-range", "3-5")
    assert result.exit_code == 0
    assert "basira ocr book.pdf --page-range 1-2 --resume" in result.stderr
    meta, pages = pages_of(ocr_output(tmp_path))
    assert sorted(pages) == [3, 4, 5]
    assert meta["processing_status"] == "Incomplete"

    assert run("ocr", pdf, "--page-range", "1-2", "--resume").exit_code == 0
    meta, pages = pages_of(ocr_output(tmp_path))
    assert sorted(pages) == [1, 2, 3, 4, 5]
    assert meta["processing_status"] == "Completed"
    assert provider.ocr_calls == [3, 4, 5, 1, 2]


def test_failed_pages_keep_the_reason_in_the_marker_and_resume_retries_them(provider, make_pdf, tmp_path):
    pdf = make_pdf(4)
    provider.fail_pages = {2, 4}

    result = run("ocr", pdf)
    assert result.exit_code == 3  # finished, some pages failed
    assert 'failed=2, 4' in result.stderr or 'failed="2, 4"' in result.stderr
    assert "basira ocr book.pdf --resume" in result.stderr
    content = ocr_output(tmp_path).read_text()
    assert "<!-- Page 2 - Error: boom on page 2 -->" in content
    _, pages = pages_of(ocr_output(tmp_path))
    assert pages[2] == ("", "boom on page 2")

    provider.fail_pages = set()
    provider.ocr_calls.clear()
    assert run("ocr", pdf, "--resume").exit_code == 0
    assert provider.ocr_calls == [2, 4]
    meta, pages = pages_of(ocr_output(tmp_path))
    assert all(error is None for _, error in pages.values())
    assert meta["processing_status"] == "Completed"


def test_existing_output_is_never_replaced_silently(provider, make_pdf, tmp_path):
    pdf = make_pdf(2)
    assert run("ocr", pdf).exit_code == 0
    output = ocr_output(tmp_path)
    output.write_text(output.read_text().replace("text of page 1", "my manual fix"))
    provider.ocr_calls.clear()

    result = run("ocr", pdf)
    assert result.exit_code == 1
    assert "already exists (2 of 2 pages done). Nothing was changed." in result.stderr
    assert "basira ocr book.pdf --resume" in result.stderr
    assert "basira ocr book.pdf --force-restart" in result.stderr
    assert "my manual fix" in output.read_text()
    assert provider.ocr_calls == []

    assert run("ocr", pdf, "--force-restart").exit_code == 0
    assert "my manual fix" not in output.read_text()


def test_refusal_repeats_the_options_of_the_original_command(provider, make_pdf):
    pdf = make_pdf(4)
    assert run("ocr", pdf, "--page-range", "2-3", "--dpi", "150").exit_code == 0
    result = run("ocr", pdf, "--page-range", "2-3", "--dpi", "150")
    assert "basira ocr book.pdf --page-range 2-3 --dpi 150 --resume" in result.stderr


def test_run_stops_after_five_failures_in_a_row(provider, make_pdf, tmp_path):
    provider.fail_pages = set(range(2, 20))
    result = run("ocr", make_pdf(12))
    assert result.exit_code == 1
    assert provider.ocr_calls == [1, 2, 3, 4, 5, 6]
    assert "5 pages in a row failed" in result.stderr
    meta, pages = pages_of(ocr_output(tmp_path))
    assert sorted(pages) == [1, 2, 3, 4, 5, 6]
    assert meta["processing_status"] == "Incomplete"


def test_authentication_failure_stops_the_run_and_leaves_the_page_missing(provider, make_pdf, tmp_path):
    provider.raise_on_page = {2: AuthenticationError("the endpoint rejected the API key (HTTP 401)")}
    result = run("ocr", make_pdf(3))
    assert result.exit_code == 1
    assert "stopped" in result.stderr and "rejected the API key" in result.stderr
    _, pages = pages_of(ocr_output(tmp_path))
    assert sorted(pages) == [1]


def test_unreachable_endpoint_fails_before_any_page(provider, make_pdf, tmp_path):
    provider.connection_error = EndpointUnreachableError("connection refused (127.0.0.1:9)")
    result = run("ocr", make_pdf(3))
    assert result.exit_code == 1
    assert "cannot reach the model endpoint: connection refused (127.0.0.1:9)" in result.stderr
    assert not ocr_output(tmp_path).exists()
    assert provider.ocr_calls == []


def test_connection_warning_is_shown_and_the_run_continues(provider, make_pdf):
    provider.connection_warning = "model 'x' was not found on fake.local; trying anyway"
    result = run("ocr", make_pdf(1))
    assert result.exit_code == 0
    assert "warning" in result.stderr and "trying anyway" in result.stderr


def test_retries_are_reported(provider, make_pdf):
    provider.retry_pages = {1}
    result = run("ocr", make_pdf(1))
    assert "ocr retry   page=1 attempt=1/2 wait=0s" in result.stderr
    assert "retries=1" in result.stderr


def test_first_ctrl_c_finishes_the_current_page_and_pauses(provider, make_pdf, tmp_path):
    provider.on_page = {2: lambda: os.kill(os.getpid(), signal.SIGINT)}
    result = run("ocr", make_pdf(4))
    assert result.exit_code == 130
    assert provider.ocr_calls == [1, 2]  # page 2 still finished
    assert "stopping after the current page" in result.stderr
    assert "ocr paused" in result.stderr
    assert result.stdout == ""
    _, pages = pages_of(ocr_output(tmp_path))
    assert sorted(pages) == [1, 2]


def test_json_log_format(provider, make_pdf):
    result = run("ocr", make_pdf(2), "--log-format", "json")
    events = [json.loads(line) for line in result.stderr.splitlines()]
    assert [e["event"] for e in events] == [
        "run_started", "page_started", "page_done", "page_started", "page_done", "run_finished",
    ]
    assert events[-1]["outcome"] == "done" and events[-1]["done"] == 2


def test_ocr_fails_when_every_page_fails(provider, make_pdf):
    provider.fail_pages = {1, 2}
    assert run("ocr", make_pdf(2)).exit_code == 1


def test_ocr_rejects_bad_input(provider, make_pdf):
    pdf = make_pdf(2)
    assert run("ocr", pdf, "--page-range", "5-6").exit_code == 2
    assert run("ocr", pdf, "--resume", "--force-restart").exit_code == 2
    result = run("ocr", pdf, "--source-language", "klingon")
    assert result.exit_code == 1 and "Available" in result.stderr


def test_missing_api_key(provider, make_pdf, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY")
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    result = run("ocr", make_pdf(1))
    assert result.exit_code == 1 and "GEMINI_API_KEY is not set" in result.stderr


# ---------------------------------------------------------------------------- translate


def write_source(tmp_path, body):
    source = tmp_path / "book.md"
    source.write_text(f'---\ntitle: "Book"\nauthor: "Someone"\nsource_language: "english"\n---\n\n{body}')
    return source


THREE_PAGES = "<!-- Page 1 -->\none\n\n---\n\n<!-- Page 2 -->\ntwo\n\n---\n\n<!-- Page 3 -->\nthree\n"


def test_translate_uses_the_previous_page_as_context(provider, tmp_path):
    source = write_source(tmp_path, THREE_PAGES)
    result = run("translate", source, "--target-language", "russian", "--custom-prompt", PROMPT)
    assert result.exit_code == 0, result.output

    assert provider.prompts == ["CTX[] SRC[one]", "CTX[tr(one)] SRC[two]", "CTX[tr(two)] SRC[three]"]
    meta, pages = pages_of(tmp_path / "book_russian.md")
    assert pages == {1: ("tr(one)", None), 2: ("tr(two)", None), 3: ("tr(three)", None)}
    assert meta["title"] == "Book" and meta["target_language"] == "russian"
    assert meta["source_file"] == "book.md"
    assert "#pagebreak()" in (tmp_path / "book_russian.md").read_text()
    assert result.stdout == "book_russian.md\n"


def test_translate_clears_the_window_after_an_error_and_resume_retries(provider, tmp_path):
    source = write_source(tmp_path, THREE_PAGES)
    provider.fail_texts = {"two"}
    assert run("translate", source, "--custom-prompt", PROMPT).exit_code == 3
    assert provider.prompts[-1] == "CTX[] SRC[three]"

    provider.fail_texts = set()
    provider.prompts.clear()
    assert run("translate", source, "--custom-prompt", PROMPT, "--resume").exit_code == 0
    assert provider.prompts == ["CTX[tr(one)] SRC[two]"]
    meta, pages = pages_of(tmp_path / "book_english.md")
    assert meta["processing_status"] == "Completed"
    assert pages[2] == ("tr(two)", None)


def test_translate_does_not_send_failed_ocr_pages(provider, tmp_path):
    source = write_source(
        tmp_path,
        "<!-- Page 1 -->\none\n\n---\n\n<!-- Page 2 - Error: HTTP 500 INTERNAL -->\n\n---\n\n"
        "<!-- Page 3 -->\n\n\n---\n\n<!-- Page 4 -->\nfour\n",
    )
    result = run("translate", source, "--custom-prompt", PROMPT)
    assert result.exit_code == 3
    assert provider.prompts == ["CTX[] SRC[one]", "CTX[] SRC[four]"]
    _, pages = pages_of(tmp_path / "book_english.md")
    assert pages[2] == ("", "no OCR text in the source (HTTP 500 INTERNAL)")
    assert pages[3] == ("", None)
    assert "fix the failed OCR pages in book.md first" in result.stderr


def test_text_written_under_an_error_marker_counts_as_fixed(provider, tmp_path):
    source = write_source(tmp_path, "<!-- Page 1 - Error: HTTP 500 INTERNAL -->\nfixed by hand\n")
    assert run("translate", source, "--custom-prompt", PROMPT).exit_code == 0
    assert provider.prompts == ["CTX[] SRC[fixed by hand]"]


def test_translate_refuses_to_replace_an_existing_translation(provider, tmp_path):
    source = write_source(tmp_path, THREE_PAGES)
    assert run("translate", source, "--custom-prompt", PROMPT).exit_code == 0
    result = run("translate", source, "--custom-prompt", PROMPT)
    assert result.exit_code == 1
    assert "book_english.md already exists (3 of 3 pages done)" in result.stderr


def test_translate_rejects_prompt_without_text(provider, tmp_path):
    source = write_source(tmp_path, "<!-- Page 1 -->\none\n")
    result = run("translate", source, "--custom-prompt", "no placeholder")
    assert result.exit_code == 1 and "{text}" in result.stderr


def test_translate_with_builtin_template(provider, tmp_path):
    source = write_source(tmp_path, "<!-- Page 1 -->\none\n")
    provider.generate_text = lambda prompt: provider.prompts.append(prompt) or "uno"
    assert run("translate", source, "--target-language", "french").exit_code == 0
    assert "one" in provider.prompts[0] and "English" in provider.prompts[0]
    assert "{" not in provider.prompts[0].replace("{{", "")
