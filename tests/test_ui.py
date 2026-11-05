import io
import json

from rich.console import Console

from basira_vision.ui import display_path
from basira_vision.ui.events import (
    ErrorReported,
    Hint,
    PageDone,
    PageFailed,
    PageRetrying,
    PageStarted,
    RunFinished,
    RunStarted,
)
from basira_vision.ui.live import LiveRenderer
from basira_vision.ui.plain import JsonRenderer, PlainRenderer
from basira_vision.ui.state import format_duration, format_pages


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
