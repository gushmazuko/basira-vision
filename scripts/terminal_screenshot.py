"""Regenerate assets/terminal.svg, the README picture of a run in progress.

The picture is drawn by the real live renderer from a scripted run, so it
always matches what the CLI shows. Run from the repository root:

    python scripts/terminal_screenshot.py
"""

import time
from pathlib import Path

from rich.console import Console
from rich.terminal_theme import MONOKAI

from basira_vision.ui.events import PageDone, PageRetrying, PageStarted, RunStarted
from basira_vision.ui.live import LiveRenderer

OUTPUT = Path(__file__).resolve().parent.parent / "assets" / "terminal.svg"


def main() -> None:
    console = Console(record=True, width=100, force_terminal=True, color_system="truecolor", highlight=False)
    renderer = LiveRenderer(console)
    renderer._start_live = lambda: None  # draw the status block once instead of animating it
    renderer._title = lambda: None

    renderer.emit(RunStarted(
        "ocr", "book.pdf", "workspace/book/book.md",
        "300 pages · 80 done earlier · prompt ocr/english · gemini-3.8-flash · 300 dpi",
        pages=list(range(1, 301)), todo=list(range(81, 301)), done_before=list(range(1, 81)),
    ))
    for page in range(81, 153):
        renderer.emit(PageStarted(page, "reading"))
        renderer.emit(PageDone(page, 2.3))
    renderer.state.started_at = time.monotonic() - 166
    renderer.emit(PageStarted(153, "reading"))
    renderer.emit(PageRetrying(153, "HTTP 429 RESOURCE_EXHAUSTED", 1, 2, 14, rate_limited=True))

    console.print(renderer._status_block())
    OUTPUT.write_text(console.export_svg(title="basira ocr", theme=MONOKAI), encoding="utf-8")
    print(f"wrote {OUTPUT}")


if __name__ == "__main__":
    main()
