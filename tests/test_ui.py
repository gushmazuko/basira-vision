import io
import json

from rich.console import Console

from basira_vision.ui import display_path
from basira_vision.ui.events import (
    CaptureStarted,
    ErrorReported,
    Hint,
    PageDone,
    PageFailed,
    PageRead,
    PageRetrying,
    PageStarted,
    PhotoReceived,
    ReadingStopped,
    RunFinished,
    RunStarted,
)
from basira_vision.ui.live import LiveRenderer
from basira_vision.ui.plain import JsonRenderer, PlainRenderer
from basira_vision.ui.state import RunState, format_duration, format_pages


def started(pages=6):
    return RunStarted("ocr", "book.pdf", "workspace/book/book.md", "6 pages", pages=list(range(1, pages + 1)),
                      todo=list(range(1, pages + 1)))


def test_helpers():
    assert format_pages([9, 3, 4, 5, 12, 13]) == "3–5, 9, 12–13"
    assert format_pages([]) == ""
    assert format_duration(59.6) == "1:00"
    assert format_duration(3725) == "1:02:05"


def test_display_path(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert display_path(tmp_path / "workspace" / "a.md") == "workspace/a.md"
    assert display_path("x.md") == "x.md"


def live_output(events) -> str:
    buffer = io.StringIO()
    renderer = LiveRenderer(Console(file=buffer, force_terminal=True, width=100, color_system=None))
    for event in events:
        renderer.emit(event)
    renderer.close()
    return buffer.getvalue()


def test_live_view_collapses_repeated_failures_and_prints_a_summary():
    text = live_output([
        started(),
        PageStarted(1), PageDone(1, 0.5),
        *[e for n in (2, 3, 4) for e in (PageStarted(n), PageFailed(n, "connection refused (gw:1)"))],
        RunFinished("ocr", "stopped", reason="3 pages in a row failed", done=1, total=6,
                    failed={2: "connection refused (gw:1)", 3: "connection refused (gw:1)",
                            4: "connection refused (gw:1)"},
                    hints=[Hint("continue", "basira ocr book.pdf --resume", command=True)]),
    ])
    assert "basira ocr  book.pdf  →  workspace/book/book.md" in text
    assert "✗ pages 2–4" in text and "×3" in text
    assert text.count("connection refused (gw:1)  ×3") == 1
    assert "✗ ocr stopped: 3 pages in a row failed" in text
    assert "1 of 6 pages saved" in text
    assert "continue  basira ocr book.pdf --resume" in text


def test_live_view_summary_for_a_clean_run_and_an_error():
    text = live_output([started(2), PageStarted(1), PageDone(1, 1), PageStarted(2), PageDone(2, 1),
                        RunFinished("ocr", "done", done=2, total=2, elapsed=2)])
    assert "✓ ocr finished   2 of 2 pages in 0:02" in text
    text = live_output([ErrorReported("GEMINI_API_KEY is not set", [Hint("set it", "export GEMINI_API_KEY=…")])])
    assert "error: GEMINI_API_KEY is not set" in text and "set it  export GEMINI_API_KEY=…" in text


URL = "http://192.168.1.20:8765/k7mqx2hd/"


def capture_started(**kwargs):
    return CaptureStarted("capture", "phone camera", "workspace/book/book.md", "prompt ocr/english", url=URL, **kwargs)


def test_live_capture_view_shows_a_qr_code_the_address_and_a_line_per_page():
    text = live_output([
        capture_started(),
        PhotoReceived(1), PageStarted(1, "reading"), PageRead(1, 12, "First line of the page\nSecond line"),
        PhotoReceived(2), PageStarted(2, "reading"), PageFailed(2, "boom"),
        PhotoReceived(3), PageStarted(3, "reading"), PageRead(3, 5, ""),
        ReadingStopped("rate limit exceeded"),
        RunFinished("capture", "stopped", reason="rate limit exceeded", done=2, total=3, failed={2: "boom"},
                    elapsed=30),
    ])
    assert "basira capture  phone camera  →  workspace/book/book.md" in text
    assert sum(text.count(block) for block in "▀▄█") > 200 and "Scan the code with the phone's camera" in text
    assert URL in text
    assert "✓ page 1  0:12  First line of the page" in text and "Second line" not in text
    assert "✓ page 3  0:05  no text on this page" in text
    assert "✗ page 2" in text and "boom" in text
    assert "! reading stopped: rate limit exceeded" in text
    assert "✗ capture stopped: rate limit exceeded" in text


def test_live_capture_view_leaves_the_code_out_when_the_terminal_is_too_narrow(monkeypatch):
    monkeypatch.setenv("TERM", "xterm-256color")  # rich gives a dumb terminal 80 columns, whatever is asked
    buffer = io.StringIO()
    renderer = LiveRenderer(Console(file=buffer, force_terminal=True, width=30, color_system=None))
    renderer.emit(capture_started())
    renderer.close()
    text = buffer.getvalue()
    assert not set("▀▄█") & set(text) and "Open this address" in text and URL in text


def capture_start_on(width, height) -> str:
    buffer = io.StringIO()
    renderer = LiveRenderer(Console(file=buffer, force_terminal=True, width=width, height=height, color_system=None))
    renderer.emit(capture_started())
    renderer.close()
    return buffer.getvalue()


def test_the_qr_code_is_only_shown_when_all_of_it_stays_on_the_screen(monkeypatch):
    monkeypatch.setenv("TERM", "xterm-256color")
    blocks = set("▀▄█")
    tall = capture_start_on(100, 50).splitlines()
    low = capture_start_on(80, 24).splitlines()
    tiny = capture_start_on(80, 15)
    for lines in (tall, low):
        code = [i for i, line in enumerate(lines) if blocks & set(line)]
        address = next(i for i, line in enumerate(lines) if URL in line)
        assert len(code) == 15 and address > code[-1]  # 17 lines with the light border above and below
    # In a low terminal the code comes last, so that it does not scroll away: code, address, status.
    code = [i for i, line in enumerate(low) if blocks & set(line)]
    assert next(i for i, line in enumerate(low) if URL in line) == code[-1] + 2  # after the border line
    assert "Scan the code" in "\n".join(low[:code[0]])
    assert not blocks & set(tiny) and "Open this address" in tiny and URL in tiny


def test_text_from_the_model_cannot_send_commands_to_the_terminal():
    text = live_output([capture_started(), PageStarted(1, "reading"), PageRead(1, 1, "title \x1b[2J\x1b[31m red\x07")])
    assert "✓ page 1  0:01  title [2J[31m red" in text


def test_capture_progress_counts_what_is_read_waiting_and_failed():
    state = RunState(capture_started(todo=[4, 5], done_before=[1, 2, 3], failed_before={9: "no photo kept"}))
    assert state.open_ended and (state.completed, len(state.waiting)) == (3, 2)
    assert state.failed == {9: "no photo kept"}
    state.failed.clear()
    for event in (PageStarted(4, "reading"), PageRead(4, 1.0, "text"), PhotoReceived(6), PhotoReceived(2, retake=True),
                  PageStarted(5, "reading"), PageFailed(5, "boom")):
        state.apply(event)
    assert (state.completed, sorted(state.waiting), dict(state.failed)) == (3, [2, 6], {5: "boom"})
    state.apply(PageStarted(6, "reading"))
    state.apply(ReadingStopped("rate limit exceeded"))
    assert state.active is None and sorted(state.waiting) == [2, 6] and state.reading_stopped == "rate limit exceeded"


def test_capture_events_as_plain_and_json_lines():
    stream = io.StringIO()
    renderer = PlainRenderer("capture", stream)
    # todo holds more than is read here: a capture session has no end to estimate, so no eta
    events = [capture_started(todo=[2, 4, 5], done_before=[1]), PhotoReceived(3), PhotoReceived(1, retake=True),
              PageStarted(3, "reading"), PageRead(3, 1.25, "text"), ReadingStopped("rate limit exceeded")]
    for event in events:
        renderer.emit(event)
    lines = [line.split(" ", 2)[2] for line in stream.getvalue().splitlines()]
    assert lines == [
        f'capture start   input="phone camera" output=workspace/book/book.md url={URL} todo=3 done_before=1 '
        'info="prompt ocr/english"',
        "capture photo   page=3",
        "capture photo   page=1 retake=yes",
        "capture ok      page=3 took=1.2s",
        'capture warning msg="reading stopped: rate limit exceeded" '
        'hint="photos taken from now on are kept; fix the cause and run the command again"',
    ]

    stream = io.StringIO()
    renderer = JsonRenderer("capture", stream)
    for event in events:
        renderer.emit(event)
    records = [json.loads(line) for line in stream.getvalue().splitlines()]
    # A capture event that extends a common one keeps the common name and adds its own fields.
    assert [r["event"] for r in records] == ["run_started", "photo_received", "photo_received", "page_started",
                                             "page_done", "reading_stopped"]
    assert records[0]["url"] == URL and records[4]["preview"] == "text" and records[2]["retake"] is True


def test_plain_lines():
    stream = io.StringIO()
    renderer = PlainRenderer("ocr", stream)
    for event in (started(2), PageStarted(1), PageRetrying(1, "HTTP 503 UNAVAILABLE", 1, 2, 2.0),
                  PageDone(1, 1.25), PageStarted(2), PageFailed(2, "HTTP 500 INTERNAL"),
                  RunFinished("ocr", "done", done=1, total=2, failed={2: "HTTP 500 INTERNAL"}, elapsed=3)):
        renderer.emit(event)
    lines = [line.split(" ", 2)[2] for line in stream.getvalue().splitlines()]
    assert lines == [
        'ocr start   input=book.pdf output=workspace/book/book.md pages=2 todo=2 info="6 pages"',
        'ocr retry   page=1 attempt=1/2 wait=2s reason="HTTP 503 UNAVAILABLE"',
        "ocr ok      page=1 progress=1/2 took=1.2s eta=0:01",
        'ocr failed  page=2 progress=1/2 reason="HTTP 500 INTERNAL"',
        "ocr done    pages=1/2 failed=2 took=0:03",
    ]


def test_json_lines():
    stream = io.StringIO()
    renderer = JsonRenderer("ocr", stream)
    renderer.emit(PageFailed(3, "boom"))
    renderer.emit(RunFinished("ocr", "done", failed={3: "boom"}))
    first, second = (json.loads(line) for line in stream.getvalue().splitlines())
    assert first["event"] == "page_failed" and first["page"] == 3
    assert second["failed"] == {"3": "boom"}


def test_short_books_get_a_stretched_bar_and_failures_stay_visible():
    text = live_output([started(3), PageStarted(1), PageDone(1, 1), PageStarted(2), PageFailed(2, "boom")])
    bar = next(line for line in text.splitlines() if "/3 pages" in line)
    assert bar.startswith("━" * 16) and "×" in bar
