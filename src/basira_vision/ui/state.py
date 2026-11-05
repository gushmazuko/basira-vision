"""Run progress shared by all renderers: counts, timing and the ETA."""

import time
from collections import deque
from typing import Dict, Optional, Set

from .events import PageDone, PageFailed, PageRetrying, PageStarted, RunStarted

ETA_WINDOW = 12  # pages in the moving average


def format_duration(seconds: float) -> str:
    seconds = int(round(max(0.0, seconds)))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def format_pages(pages) -> str:
    """``[3, 4, 5, 9]`` -> ``3–5, 9``."""
    numbers = sorted(pages)
    parts, start, prev = [], None, None
    for n in numbers + [None]:
        if start is None:
            start = prev = n
        elif n is not None and n == prev + 1:
            prev = n
        else:
            parts.append(str(start) if start == prev else f"{start}–{prev}")
            start = prev = n
    return ", ".join(parts)


def pages_label(pages) -> str:
    """``page 4`` or ``pages 4–6, 9``."""
    pages = list(pages)
    return f"page {pages[0]}" if len(pages) == 1 else f"pages {format_pages(pages)}"


class RunState:
    def __init__(self, started: RunStarted):
        self.stage = started.stage
        self.pages = list(started.pages or [])
        self.total = started.total
        self.todo = list(started.todo)
        self.done_before: Set[int] = set(started.done_before)
        self.done: Set[int] = set()
        self.failed: Dict[int, str] = {}
        self.active: Optional[int] = None
        self.activity = ""
        self.retry: Optional[PageRetrying] = None
        self.retry_until = 0.0
        self.retries = 0
        self.stopping = False
        self.started_at = time.monotonic()
        self._durations = deque(maxlen=ETA_WINDOW)

    def apply(self, event) -> None:
        if isinstance(event, PageStarted):
            self.active, self.activity, self.retry = event.page, event.activity, None
        elif isinstance(event, PageRetrying):
            self.retry, self.retry_until = event, time.monotonic() + event.wait
            self.retries += 1
        elif isinstance(event, PageDone):
            self.done.add(event.page)
            self.failed.pop(event.page, None)
            self._finish_page(event.seconds)
        elif isinstance(event, PageFailed):
            self.failed[event.page] = event.reason
            self._finish_page(event.seconds)

    def _finish_page(self, seconds: float) -> None:
        self._durations.append(seconds)
        self.active, self.retry = None, None

    @property
    def completed(self) -> int:
        return len(self.done | self.done_before)

    @property
    def processed(self) -> int:
        return len(self.done) + len(self.failed)

    def elapsed(self) -> float:
        return time.monotonic() - self.started_at

    def eta(self) -> Optional[float]:
        remaining = len(self.todo) - self.processed
        if not self._durations or remaining <= 0:
            return None
        return remaining * sum(self._durations) / len(self._durations)

    def retry_left(self) -> float:
        return max(0.0, self.retry_until - time.monotonic())
