"""Every command keeps a log next to the file it writes: <output>.log, plain lines, appended."""

import os
import signal

import pytest
from click.testing import CliRunner

from basira_vision import __version__
from basira_vision.cli import main
from basira_vision.processing import typst_processor
from basira_vision.ui import Reporter
from basira_vision.ui.events import Notice, PageRead, PageStarted
from basira_vision.ui.journal import Journal
from basira_vision.utils.errors import AuthenticationError, EndpointUnreachableError
from basira_vision.utils.logging import get_logger

PROMPT = "CTX[{context_section}] SRC[{text}]"


def run(*args):
    return CliRunner().invoke(main, [str(a) for a in args], catch_exceptions=False)


def lines_of(path):
    """The lines of a log without their timestamps: ``ocr ok      page=1 ...``."""
    return [line.split(" ", 2)[2] for line in path.read_text(encoding="utf-8").splitlines()]


def kinds(path):
    """``["run", "start", "ok", ...]``: what each line of a log is."""
    return [line.split()[1] for line in lines_of(path)]


def book_dir(tmp_path):
    return tmp_path / "workspace" / "book"


# ---------------------------------------------------------------------------- ocr


def test_ocr_keeps_a_log_next_to_the_book(provider, make_pdf, tmp_path):
    result = run("ocr", make_pdf(2), "--strips", "3")
    assert result.exit_code == 0, result.output

    log = lines_of(book_dir(tmp_path) / "book.log")
    assert log[0].startswith(f"ocr run     basira={__version__} pid={os.getpid()} ")
    assert log[0].endswith('command="basira ocr book.pdf --strips 3"')
    assert log[1].startswith("ocr start   input=book.pdf output=workspace/book/book.md pages=2 todo=2 ")
    assert log[2].startswith("ocr ok      page=1 progress=1/2 ") and log[3].startswith("ocr ok      page=2 ")
    assert log[4].startswith("ocr done    pages=2/2 ")
    assert all(line.startswith("ocr hint    ") for line in log[5:]) and len(log) > 5
    # What the terminal showed is all there, and the terminal shows what it always did.
    shown = [line.split(" ", 2)[2].split(" took=")[0] for line in result.stderr.splitlines()]
    assert shown == [line.split(" took=")[0] for line in log[1:]]
    assert "text of page" not in "\n".join(log)  # the book is in the book, not in its log
    assert result.stdout == "workspace/book/book.md\n"


def test_every_run_is_added_to_the_same_log(provider, make_pdf, tmp_path):
    pdf = make_pdf(3)
    provider.fail_pages = {2}
    assert run("ocr", pdf).exit_code == 3
    first = (book_dir(tmp_path) / "book.log").read_text()

    provider.fail_pages = set()
    assert run("ocr", pdf, "--resume").exit_code == 0
    whole = (book_dir(tmp_path) / "book.log").read_text()
    assert whole.startswith(first)
    assert kinds(book_dir(tmp_path) / "book.log").count("run") == 2
    log = lines_of(book_dir(tmp_path) / "book.log")
    assert any(line.startswith('ocr failed  page=2 progress=1/3 reason="boom on page 2"') for line in log)
    assert log[-1].startswith("ocr hint    ") and any('command="basira ocr book.pdf --resume"' in line for line in log)


@pytest.mark.parametrize("log_format", ["json", "live"])
def test_the_log_is_plain_lines_whatever_the_terminal_shows(provider, make_pdf, tmp_path, log_format):
    assert run("ocr", make_pdf(1), "--log-format", log_format).exit_code == 0
    assert kinds(book_dir(tmp_path) / "book.log")[:4] == ["run", "start", "ok", "done"]


@pytest.mark.parametrize("problem", ["no key", "endpoint down", "key rejected"])
def test_a_first_run_that_cannot_start_leaves_nothing_behind(provider, make_pdf, tmp_path, monkeypatch, problem):
    if problem == "no key":
        monkeypatch.delenv("GEMINI_API_KEY")
    elif problem == "endpoint down":
        provider.connection_error = EndpointUnreachableError("connection refused (gw:1)")
    else:
        provider.connection_error = AuthenticationError("the endpoint rejected the API key (HTTP 401)")
    assert run("ocr", make_pdf(1)).exit_code == 1
    assert not (tmp_path / "workspace").exists()


def test_a_refused_run_on_an_existing_book_is_in_its_log(provider, make_pdf, tmp_path):
    pdf = make_pdf(2)
    assert run("ocr", pdf).exit_code == 0
    book = (book_dir(tmp_path) / "book.md").read_text()

    result = run("ocr", pdf)  # neither --resume nor --overwrite
    assert result.exit_code == 1 and "already exists" in result.stderr
    log = lines_of(book_dir(tmp_path) / "book.log")
    again = log[len(log) - 1 - log[::-1].index(next(line for line in reversed(log) if " run " in line)):]
    assert [line.split()[1] for line in again] == ["run", "error", "hint", "hint"]
    assert "already exists (2 of 2 pages done). Nothing was changed." in again[1]
    assert (book_dir(tmp_path) / "book.md").read_text() == book


def test_what_went_before_the_book_existed_is_in_the_log_too(provider, make_pdf, tmp_path):
    provider.connection_warning = "model 'x' was not found on gw (HTTP 404); trying anyway"
    assert run("ocr", make_pdf(1)).exit_code == 0
    assert kinds(book_dir(tmp_path) / "book.log")[:3] == ["run", "warning", "start"]


def test_no_log_writes_no_log(provider, make_pdf, tmp_path):
    result = run("ocr", make_pdf(1), "--no-log")
    assert result.exit_code == 0, result.output
    assert sorted(p.name for p in book_dir(tmp_path).iterdir()) == ["book.md", "images"]
    assert "--no-log" in run("ocr", "--help").output
    assert "--no-log" not in run("export", "template", "--help").output  # it writes no book


def test_a_log_that_cannot_be_written_is_one_warning_and_the_run_goes_on(provider, make_pdf, tmp_path):
    pdf = make_pdf(2)
    assert run("ocr", pdf, "--page-range", "1").exit_code == 0
    log = book_dir(tmp_path) / "book.log"
    log.unlink()
    log.mkdir()  # something else has that name

    result = run("ocr", pdf, "--resume")
    assert result.exit_code == 0, result.output
    assert result.stderr.count("the log cannot be written") == 1
    assert "workspace/book/book.log" in result.stderr and "ocr done    pages=2/2" in result.stderr


def test_ctrl_c_is_in_the_log(provider, make_pdf, tmp_path):
    provider.on_page = {2: lambda: os.kill(os.getpid(), signal.SIGINT)}
    assert run("ocr", make_pdf(4)).exit_code == 130
    assert kinds(book_dir(tmp_path) / "book.log")[:6] == ["run", "start", "ok", "stop", "ok", "paused"]


def test_an_unexpected_error_is_in_the_log(provider, make_pdf, tmp_path):
    provider.raise_on_page = {2: RuntimeError("something nobody planned for")}
    with pytest.raises(RuntimeError):
        run("ocr", make_pdf(2))
    log = lines_of(book_dir(tmp_path) / "book.log")
    assert log[-1] == 'ocr error   msg="stopped by an unexpected error: RuntimeError: something nobody planned for"'


def test_the_command_in_the_log_is_the_one_that_was_typed(provider, make_pdf, tmp_path, monkeypatch):
    prompt_file = tmp_path / "my prompt.txt"
    prompt_file.write_text("Read the page.")
    pdf = make_pdf(1)
    assert run("ocr", pdf, "--source-language", "french", "-m", "model-a", "--custom-prompt", prompt_file,
               "--no-images", "--verbose").exit_code == 0
    log = lines_of(book_dir(tmp_path) / "book.log")
    assert log[0].endswith("command=\"basira ocr book.pdf --source-language french --model model-a "
                           "--custom-prompt 'my prompt.txt' --no-images --verbose\"")

    # Prompt text given on the command line is not copied into the log, only that there was one.
    assert run("ocr", pdf, "--overwrite", "--custom-prompt", "Read every\nline of the page.").exit_code == 0
    log = lines_of(book_dir(tmp_path) / "book.log")
    assert log[-1 - log[::-1].index(next(line for line in reversed(log) if " run " in line))].endswith(
        'command="basira ocr book.pdf --custom-prompt <text> --overwrite"')
    assert "Read every" not in (book_dir(tmp_path) / "book.log").read_text()


# ---------------------------------------------------------------------------- translate, typst


def test_translate_keeps_the_log_of_its_own_file(provider, make_pdf, tmp_path):
    assert run("ocr", make_pdf(2)).exit_code == 0
    source = book_dir(tmp_path) / "book.md"
    assert run("translate", source, "--custom-prompt", PROMPT).exit_code == 0

    log = lines_of(book_dir(tmp_path) / "book_english.log")
    assert log[0].endswith('command="basira translate workspace/book/book.md --custom-prompt <text>"')
    assert [line.split()[1] for line in log][:4] == ["run", "start", "ok", "ok"]
    assert all(line.startswith("translate ") for line in log)
    assert "tr(" not in "\n".join(log) and "text of page" not in "\n".join(log)
    assert kinds(book_dir(tmp_path) / "book.log").count("run") == 1  # the source's log is the source's


def test_a_translation_that_cannot_start_leaves_no_log(provider, make_pdf, tmp_path):
    assert run("ocr", make_pdf(1)).exit_code == 0
    source = book_dir(tmp_path) / "book.md"

    provider.connection_error = EndpointUnreachableError("connection refused (gw:1)")
    assert run("translate", source, "--custom-prompt", PROMPT).exit_code == 1
    provider.connection_error = None
    assert run("translate", source, "--custom-prompt", PROMPT, "--resume").exit_code == 1  # nothing to resume
    assert sorted(p.name for p in book_dir(tmp_path).iterdir()) == ["book.log", "book.md", "images"]


def test_an_output_named_like_a_log_does_not_get_the_log_written_into_it(provider, make_pdf, tmp_path):
    assert run("ocr", make_pdf(1)).exit_code == 0
    out = tmp_path / "translation.log"
    assert run("translate", book_dir(tmp_path) / "book.md", "--custom-prompt", PROMPT, "-o", out).exit_code == 0
    assert out.read_text().startswith("---\n")  # the translation itself
    assert kinds(tmp_path / "translation.log.log")[0] == "run"


needs_tools = pytest.mark.skipif(typst_processor.missing_tools(), reason="pandoc and typst are required")


@needs_tools
def test_typst_adds_to_the_log_of_the_edition_it_typesets(provider, make_pdf, tmp_path):
    assert run("ocr", make_pdf(1)).exit_code == 0
    assert run("translate", book_dir(tmp_path) / "book.md", "--custom-prompt", PROMPT).exit_code == 0
    edition = book_dir(tmp_path) / "book_english.md"

    assert run("typst", edition).exit_code == 1  # no title and author yet: said in the edition's log
    assert run("typst", edition, "--title", "A Book", "--author", "Someone").exit_code == 0
    log = lines_of(book_dir(tmp_path) / "book_english.log")
    assert [line.split()[0] for line in log if " run " in line] == ["translate", "typst", "typst"]
    assert any(line.startswith('typst error   msg="no title and author for') for line in log)
    assert log[-1].startswith("typst hint    label=output text=workspace/book/book_english.pdf")
    assert any("command=\"basira typst workspace/book/book_english.md --title 'A Book' --author Someone\"" in line
               for line in log)


@needs_tools
def test_typst_on_a_file_of_its_own_leaves_no_log_when_it_cannot_start(provider, tmp_path):
    source = tmp_path / "essay.md"
    source.write_text("# An essay\n\nText.\n")
    assert run("typst", source).exit_code == 1  # no title and author
    assert sorted(p.name for p in tmp_path.iterdir()) == ["essay.md"]
    assert run("typst", source, "--title", "An Essay", "--author", "Someone").exit_code == 0
    assert kinds(tmp_path / "essay.log")[0] == "run"


# ---------------------------------------------------------------------------- what goes where


def reporter_with_log(tmp_path, capsys, verbose):
    journal = Journal("ocr", basira="0.0.0", command="basira ocr book.pdf")
    reporter = Reporter("ocr", "plain", verbose, journal)
    with reporter:
        output = tmp_path / "book.md"
        output.write_text("")
        reporter.log_to(output)
        logger = get_logger("test")
        logger.debug("a detail for debugging")
        logger.info("usage page_001.png: prompt=10 thoughts=0 output=5")
        logger.warning("something to look at")
        reporter.emit(PageStarted(1, "reading"))
        reporter.emit(PageRead(1, 2.0, "The first words of the page"))
    return lines_of(tmp_path / "book.log"), [line.split(" ", 2)[2] for line in capsys.readouterr().err.splitlines()]


def test_the_log_keeps_more_than_the_terminal_shows_and_never_the_text(tmp_path, capsys):
    log, terminal = reporter_with_log(tmp_path, capsys, verbose=False)
    assert terminal == ['ocr warning msg="something to look at"', "ocr ok      page=1 took=2.0s"]
    assert log[1:] == ['ocr info    msg="usage page_001.png: prompt=10 thoughts=0 output=5"',
                       'ocr warning msg="something to look at"', "ocr ok      page=1 took=2.0s"]
    assert "first words" not in "\n".join(log)


def test_verbose_adds_the_details_to_both(tmp_path, capsys):
    log, terminal = reporter_with_log(tmp_path, capsys, verbose=True)
    # On the terminal everything below a warning is a detail, as it always was.
    assert terminal == ['ocr debug   msg="a detail for debugging"',
                        'ocr debug   msg="usage page_001.png: prompt=10 thoughts=0 output=5"',
                        'ocr warning msg="something to look at"', "ocr ok      page=1 took=2.0s"]
    assert log[1:3] == ['ocr debug   msg="a detail for debugging"',
                        'ocr info    msg="usage page_001.png: prompt=10 thoughts=0 output=5"']


def test_a_line_written_from_inside_another_line_does_not_break_the_log(tmp_path):
    """Ctrl-C can arrive in the middle of writing a line; its own line must still get through."""
    journal = Journal("ocr", basira="0.0.0", command="basira ocr book.pdf")
    output = tmp_path / "book.md"
    output.write_text("")
    journal.attach(output.with_suffix(".log"), at_once=True)
    real_write = os.write
    nested = []

    def write_and_interrupt(fd, data):
        if not nested:
            nested.append(True)
            journal.emit(Notice("warning", "from the signal handler"))
        return real_write(fd, data)

    try:
        os.write = write_and_interrupt
        journal.emit(Notice("warning", "the line being written"))
    finally:
        os.write = real_write
    journal.close()
    assert lines_of(tmp_path / "book.log")[1:] == ['ocr warning msg="from the signal handler"',
                                                   'ocr warning msg="the line being written"']


def test_a_log_is_not_started_before_it_is_told_to(tmp_path):
    journal = Journal("ocr", basira="0.0.0", command="basira ocr book.pdf")
    log = tmp_path / "new" / "book.log"
    journal.attach(log, at_once=False)
    journal.emit(Notice("warning", "kept in memory"))
    assert not log.parent.exists()
    journal.close()  # the run ended before it wrote anything: no log either
    assert not log.parent.exists()

    journal = Journal("ocr", basira="0.0.0", command="basira ocr book.pdf")
    journal.attach(log, at_once=False)
    journal.emit(Notice("warning", "kept in memory"))
    log.parent.mkdir()
    journal.start()
    journal.emit(Notice("warning", "written at once"))
    assert kinds(log) == ["run", "warning", "warning"]  # also before close: every line is on disk as it is written
    journal.close()


def test_a_log_file_is_utf8_whatever_the_locale(tmp_path):
    journal = Journal("translate", basira="0.0.0", command="basira translate книга.md")
    journal.attach(tmp_path / "книга.log", at_once=True)
    journal.emit(Notice("warning", "english → russian, pages 2–4, Ӏ"))
    journal.close()
    assert "english → russian, pages 2–4, Ӏ" in open(tmp_path / "книга.log", encoding="utf-8").read()
