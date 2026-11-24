"""Interactive terminal view: a pinned status block with notable events scrolling above it."""

import math
import time
from typing import List, Optional

from rich.console import Console, Group
from rich.live import Live
from rich.measure import Measurement
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text

from .events import (
    CaptureStarted,
    ErrorReported,
    Hint,
    Notice,
    PageDone,
    PageFailed,
    PageRead,
    PageStarted,
    ReadingStopped,
    RunFinished,
    RunStarted,
    StepDone,
    StepStarted,
    StopRequested,
    kinds,
)
from .qr import QUIET_ZONE, QRCode, encode
from .state import RunState, format_duration, pages_label

ACCENT, DIM = "cyan", "grey50"
DONE, DONE_BEFORE, FAILED, ACTIVE, PENDING = "green", "dark_green", "red", "yellow", "grey30"
MAX_FAILED_ROWS = 8
# Lines a capture session shows below its QR code: the pinned status (3) and the address, or
# those plus the spacing and the two lines of instructions.
_BELOW_COMPACT, _BELOW_ROOMY = 4, 8


class PageMap:
    """A progress bar whose cells are slices of the book, colored by state.

    Without color the glyphs still differ: ━ done, × failed, ─ pending.
    """

    def __init__(self, state: RunState):
        self.state = state

    def _layout(self, max_width: int):
        """(pages per cell, characters per cell, cells): long books are binned, short ones stretched."""
        width = max(10, min(max_width - 36, 50))
        total = self.state.total
        if total >= width:
            per_cell = math.ceil(total / width)
            return per_cell, 1, math.ceil(total / per_cell)
        return 1, width // total, total

    def __rich_measure__(self, console, options):
        _, chars, cells = self._layout(options.max_width)
        return Measurement(chars * cells, chars * cells)

    def __rich_console__(self, console, options):
        state = self.state
        per_cell, chars, cells = self._layout(console.width)
        bar = Text()
        for cell in range(cells):
            pages = state.pages[cell * per_cell:(cell + 1) * per_cell]
            if any(p in state.failed for p in pages):
                glyph, style = "×", FAILED
            elif state.active in pages:
                glyph, style = "━", ACTIVE
            elif all(p in state.done or p in state.done_before for p in pages):
                glyph, style = "━", DONE if any(p in state.done for p in pages) else DONE_BEFORE
            elif any(p in state.done or p in state.done_before for p in pages):
                glyph, style = "╸", DONE
            else:
                glyph, style = "─", PENDING
            # A stretched failed page reads as ━━×━━ rather than a run of crosses.
            if glyph == "×" and chars > 1:
                half = chars // 2
                bar.append("━" * half + "×" + "━" * (chars - half - 1), FAILED)
            else:
                bar.append(glyph * chars, style)
        yield bar


class LiveRenderer:
    def __init__(self, console: Optional[Console] = None):
        self.console = console or Console(stderr=True, highlight=False)
        self.state: Optional[RunState] = None
        self.live: Optional[Live] = None
        self.step: Optional[str] = None
        self._failure_group: Optional[List] = None  # [reason, pages]

    # ------------------------------------------------------------------ lifecycle

    def emit(self, event) -> None:
        handler = next((h for h in (getattr(self, f"_on_{k.__name__}", None) for k in kinds(event)) if h), None)
        if self.state is not None:
            self.state.apply(event)
        if handler:
            handler(event)

    def close(self) -> None:
        self._stop_live()

    def _start_live(self) -> None:
        if self.live is None:
            self.live = Live(
                console=self.console,
                get_renderable=self._status_block,
                refresh_per_second=10,
                transient=True,
            )
            self.live.start()

    def _stop_live(self) -> None:
        self._flush_failures()
        if self.live is not None:
            self.live.stop()
            self.live = None

    def _print(self, renderable) -> None:
        self.console.print(renderable)

    # ------------------------------------------------------------------ events

    def _on_RunStarted(self, event: RunStarted) -> None:
        self.state = RunState(event)
        self._header(event)
        self._title()
        self._start_live()

    def _header(self, event: RunStarted) -> None:
        line = Text()
        line.append(f"basira {event.stage}  ", f"bold {ACCENT}")
        line.append(event.source, "bold")
        line.append("  →  ", DIM)
        line.append(event.output, "bold")
        self._print(line)
        self._print(Text(" " * (len(event.stage) + 9) + event.details, DIM))

    def _on_CaptureStarted(self, event: CaptureStarted) -> None:
        self.state = RunState(event)
        self._header(event)
        self._print(Text(""))
        code = encode(event.url)
        size = len(code) + 2 * QUIET_ZONE if code is not None else 0
        rows = (size + 1) // 2
        note = "The phone has to be on the same network as this computer. Ctrl-C ends the session."
        # The code is only of use when all of it is on the screen together with what follows it.
        if code is None or size + 2 > self.console.width or rows + _BELOW_COMPACT > self.console.height:
            self._print(Text("  Open this address in the phone's browser:"))
            self._address(event.url)
            self._print(Text(f"  {note}", DIM))
        elif rows + _BELOW_ROOMY > self.console.height:  # a low terminal: the code last, little below it
            self._print(Text("  Scan the code with the phone's camera, or open the address below it "
                             "in its browser."))
            self._print(Text(f"  {note}", DIM))
            self._print(QRCode(code))
            self._address(event.url)
        else:
            self._print(QRCode(code))
            self._print(Text(""))
            self._print(Text("  Scan the code with the phone's camera, or open this address in its browser:"))
            self._address(event.url)
            self._print(Text(f"  {note}", DIM))
            self._print(Text(""))
        self.console.set_window_title(f"basira {event.stage}")
        self._start_live()

    def _address(self, url: str) -> None:
        # Never wrapped: an address broken over two lines cannot be copied or clicked.
        self.console.print(Text(f"  {url}", f"bold {ACCENT}"), soft_wrap=True)

    def _on_PageRead(self, event: PageRead) -> None:
        self._flush_failures()
        line = Text.assemble(("  ✓ ", DONE), (f"page {event.page}", "bold"),
                             (f"  {format_duration(event.seconds)}", DIM))
        first = event.preview.splitlines()[0] if event.preview else "no text on this page"
        first = "".join(c for c in first if c.isprintable())  # the model's text, not terminal commands
        room = self.console.width - line.cell_len - 3
        if room > 10:
            line.append("  ")
            line.append(Text(first, DIM, no_wrap=True, overflow="ellipsis", end=""))
            line.truncate(self.console.width - 1, overflow="ellipsis")
        self._print(line)

    def _on_ReadingStopped(self, event: ReadingStopped) -> None:
        self._flush_failures()
        self._print(Text.assemble(("  ! ", f"bold {ACTIVE}"), (f"reading stopped: {event.reason}", ACTIVE)))
        self._print(Text("    photos taken from now on are kept; fix the cause and run the command again", DIM))

    def _on_PageStarted(self, event: PageStarted) -> None:
        self._title()

    def _on_PageDone(self, event: PageDone) -> None:
        self._flush_failures()
        self._title()

    def _on_PageFailed(self, event: PageFailed) -> None:
        group = self._failure_group
        if group and group[0] == event.reason and group[1][-1] == event.page - 1:
            group[1].append(event.page)
        else:
            self._flush_failures()
            self._failure_group = [event.reason, [event.page]]

    def _on_StopRequested(self, event: StopRequested) -> None:
        if self.state:
            self.state.stopping = True

    def _on_StepStarted(self, event: StepStarted) -> None:
        self.step = event.name
        self._start_live()

    def _on_StepDone(self, event: StepDone) -> None:
        self.step = None
        self._print(Text.assemble(("  ✓ ", DONE), event.name, (f"  {format_duration(event.seconds)}", DIM)))

    def _on_Notice(self, event: Notice) -> None:
        self._flush_failures()
        if event.level == "debug":
            self._print(Text(f"  · {event.message}", DIM))
            return
        style = ACTIVE if event.level == "warning" else ACCENT
        mark = "!" if event.level == "warning" else "i"
        self._print(Text.assemble((f"  {mark} ", f"bold {style}"), (event.message, style)))
        if event.hint:
            self._print(Text(f"    {event.hint}", DIM))

    def _on_RunFinished(self, event: RunFinished) -> None:
        self._stop_live()
        self._print(Text(""))
        self._print(self._headline(event))
        rows = self._failed_rows(event.failed) + [self._hint_row(h) for h in event.hints]
        if event.retries:
            rows.append(("retries", Text(str(event.retries), DIM), Text("")))
        if rows:
            self._print(Text(""))
            self._print(_grid(rows, self.console.width))
        self.console.set_window_title(f"basira {event.stage}: {event.outcome}")

    def _on_ErrorReported(self, event: ErrorReported) -> None:
        self._stop_live()
        self._print(Text.assemble(("error: ", "bold red"), event.message))
        if event.hints:
            self._print(_grid([self._hint_row(h) for h in event.hints], self.console.width))

    # ------------------------------------------------------------------ pieces

    def _headline(self, event: RunFinished) -> Text:
        elapsed = format_duration(event.elapsed)
        pages = f"{event.done} of {event.total} pages" if event.total is not None else ""
        text = Text()
        if event.outcome == "done" and not event.failed:
            text.append(" ✓ ", f"bold {DONE}")
            text.append(f"{event.stage} finished", "bold")
            detail = f"{pages} in {elapsed}" if pages else " · ".join(x for x in (event.extra, elapsed) if x)
        elif event.outcome == "done":
            text.append(" ✓ ", f"bold {ACTIVE}")
            text.append(f"{event.stage} finished, {pages_label(event.failed)} failed", "bold")
            detail = f"{pages} in {elapsed}"
        elif event.outcome == "paused":
            text.append(" ⏸ ", f"bold {ACTIVE}")
            text.append(f"{event.stage} paused", "bold")
            detail = f"{pages} saved, nothing lost"
        else:
            text.append(" ✗ ", f"bold {FAILED}")
            text.append(f"{event.stage} stopped: {event.reason}", "bold")
            detail = f"{pages} saved" if pages else ""
        if detail:
            text.append(f"   {detail}", DIM)
        return text

    @staticmethod
    def _failed_rows(failed) -> list:
        by_reason = {}
        for page, reason in sorted(failed.items()):
            by_reason.setdefault(reason, []).append(page)
        rows = []
        for reason, pages in list(by_reason.items())[:MAX_FAILED_ROWS]:
            rows.append(("failed", Text.assemble((pages_label(pages), "bold"), f"  {reason}"), Text("")))
        hidden = len(by_reason) - MAX_FAILED_ROWS
        if hidden > 0:
            rows.append(("", Text(f"… {hidden} more reason(s), see the output file", DIM), Text("")))
        return rows

    @staticmethod
    def _hint_row(hint: Hint):
        return hint.label, Text(hint.text, ACCENT if hint.command else ""), Text(hint.note, DIM)

    def _flush_failures(self) -> None:
        if self._failure_group:
            self._print(_failure_line(*self._failure_group))
            self._failure_group = None

    def _status_block(self):
        parts = [Text("")]
        if self._failure_group:
            parts.insert(0, _failure_line(*self._failure_group))
        state = self.state
        if state is not None and state.total:
            parts.append(self._progress_line(state))
        elif state is not None and state.open_ended:
            parts.append(self._capture_line(state))
        activity = self._activity()
        if activity is not None:
            row = Table.grid(padding=(0, 1))
            busy = state is not None and (state.stopping or state.retry is not None)
            row.add_row(Spinner("dots", style=ACTIVE if busy else ACCENT), activity)
            parts.append(row)
        return Group(*parts)

    def _progress_line(self, state: RunState):
        counts = Text()
        counts.append(f" {state.completed}", "bold")
        counts.append(f"/{state.total} pages", DIM)
        if state.failed:
            counts.append(f"  ✗ {len(state.failed)}", FAILED)
        timing = Text(f"  {format_duration(state.elapsed())}", DIM)
        eta = state.eta()
        if eta is not None:
            timing.append(f" · ~{format_duration(eta)} left", DIM)
        line = Table.grid()
        line.add_row(PageMap(state), counts, timing)
        return line

    def _capture_line(self, state: RunState) -> Text:
        """``12 read · 2 waiting · 1 failed   5:03``, and what the session waits for when it is idle."""
        line = Text(" ")
        line.append(f" {state.completed}", "bold")
        line.append(" read", DIM)
        if state.waiting:
            line.append("  ·  ", DIM)
            line.append(str(len(state.waiting)), f"bold {ACTIVE}")
            line.append(" waiting", DIM)
        if state.failed:
            line.append("  ·  ", DIM)
            line.append(f"✗ {len(state.failed)} failed", FAILED)
        line.append(f"   {format_duration(state.elapsed())}", DIM)
        if state.active is None:
            if state.reading_stopped:
                idle = "reading stopped; photos are still taken and kept · Ctrl-C ends the session"
            elif state.stopping:
                idle = "ending the session"
            else:
                idle = "ready for photos · Ctrl-C ends the session"
            line.append(f"\n  {idle}", ACTIVE if state.reading_stopped else DIM)
        return line

    def _activity(self) -> Optional[Text]:
        state = self.state
        if self.step:
            return Text(self.step)
        if state is None or state.active is None:
            return None
        if state.stopping:
            return Text(f"stopping after page {state.active}… Ctrl-C again to stop now", ACTIVE)
        retry = state.retry
        if retry is not None:
            left = math.ceil(state.retry_left())
            if retry.rate_limited:
                return Text(f"page {retry.page} · rate limited by the API · waiting {left}s", ACTIVE)
            return Text(f"page {retry.page} · {retry.reason} · retry {retry.attempt}/{retry.attempts} in {left}s",
                        ACTIVE)
        text = Text(f"page {state.active}")
        if state.activity:
            text.append(" · ")
            text.append_text(Text.from_markup(state.activity, style=DIM))
        if state.open_ended:
            text.append(f" · {format_duration(time.monotonic() - state.active_since)}", DIM)
        return text

    def _title(self) -> None:
        state = self.state
        if state and state.total:
            self.console.set_window_title(f"basira {state.stage} {100 * state.completed // state.total}%")


def _failure_line(reason: str, pages: List[int]) -> Text:
    line = Text("  ✗ ", FAILED)
    line.append(f"{pages_label(pages):<11}", "bold")
    line.append(f" {reason}", FAILED)
    if len(pages) > 1:
        line.append(f"  ×{len(pages)}", DIM)
    return line


def _grid(rows, width: int) -> Table:
    """Labelled lines. A note follows its value when the line fits, otherwise it goes below it."""
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style=DIM, justify="right", min_width=8, no_wrap=True)
    grid.add_column(overflow="fold")  # never crop a command, wrap it
    label_width = max([8] + [len(label) for label, _, _ in rows]) + 2
    for label, text, note in rows:
        if not note.plain:
            grid.add_row(label, text)
        elif label_width + text.cell_len + 3 + note.cell_len <= width:
            grid.add_row(label, Text.assemble(text, "   ", note))
        else:
            grid.add_row(label, text)
            grid.add_row("", note)
    return grid
