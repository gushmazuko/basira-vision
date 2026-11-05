"""The page loop shared by ``ocr`` and ``translate``.

It writes every page to the page file as soon as it is done, reports
progress as events, stops cleanly on Ctrl-C, and gives up when the endpoint
is clearly down instead of failing every remaining page.
"""

import signal
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Set, Union

from .providers import VisionProvider
from .ui import Reporter
from .ui.events import PageDone, PageFailed, PageRetrying, PageStarted, StopRequested
from .utils.errors import AuthenticationError, BasiraError, RateLimitError
from .utils.pages import PageFile

MAX_CONSECUTIVE_FAILURES = 5


@dataclass
class PageTask:
    """One page to produce. ``work`` returns the page text; ``skip_reason`` fails it without a request.

    ``activity`` describes the work for the progress display; a callable is
    evaluated when the page starts, so it can describe the current state.
    """

    page: int
    work: Optional[Callable[[], str]] = None
    activity: Union[str, Callable[[], str]] = ""
    skip_reason: Optional[str] = None


@dataclass
class RunResult:
    outcome: str  # done | paused | stopped
    reason: Optional[str] = None
    done: Set[int] = field(default_factory=set)
    failed: Dict[int, str] = field(default_factory=dict)
    elapsed: float = 0.0
    retries: int = 0

    def exit_code(self) -> int:
        """0 all done, 3 done with failed pages, 1 stopped or nothing succeeded, 130 paused."""
        if self.outcome == "paused":
            return 130
        if self.outcome == "stopped":
            return 1
        if self.failed:
            return 1 if not self.done else 3
        return 0


class GracefulInterrupt:
    """First Ctrl-C asks the loop to stop after the current page; the second one stops now."""

    def __init__(self, on_request: Callable[[], None]):
        self.requested = False
        self._on_request = on_request
        self._previous = None

    def __enter__(self) -> "GracefulInterrupt":
        if threading.current_thread() is threading.main_thread():
            self._previous = signal.signal(signal.SIGINT, self._handle)
        return self

    def __exit__(self, *exc) -> None:
        if self._previous is not None:
            signal.signal(signal.SIGINT, self._previous)

    def _handle(self, signum, frame) -> None:
        if self.requested:
            raise KeyboardInterrupt
        self.requested = True
        self._on_request()


def run_pages(
    tasks: List[PageTask],
    page_file: PageFile,
    provider: VisionProvider,
    reporter: Reporter,
    expected_pages: Set[int],
) -> RunResult:
    """
    Produce each task's page and save it immediately.

    Args:
        tasks: Pages to produce, in order
        page_file: Output file; finished and failed pages are written into it
        provider: Model provider, whose retries are reported per page
        reporter: Receives progress events
        expected_pages: Every page the finished file should contain; decides
            the ``processing_status`` written to the frontmatter
    """
    result = RunResult(outcome="done")
    started = time.monotonic()
    current: List[Optional[int]] = [None]
    consecutive_failures = 0

    def on_retry(reason: str, attempt: int, attempts: int, wait: float, rate_limited: bool) -> None:
        result.retries += 1
        reporter.emit(PageRetrying(current[0], reason, attempt, attempts, wait, rate_limited))

    provider.on_retry = on_retry
    interrupt = GracefulInterrupt(lambda: reporter.emit(StopRequested(current[0])))
    try:
        with interrupt:
            for task in tasks:
                if interrupt.requested:
                    result.outcome = "paused"
                    break
                current[0] = task.page
                activity = task.activity() if callable(task.activity) else task.activity
                reporter.emit(PageStarted(task.page, activity))
                page_started = time.monotonic()

                if task.skip_reason:
                    page_file.fail_page(task.page, task.skip_reason)
                    result.failed[task.page] = task.skip_reason
                    reporter.emit(PageFailed(task.page, task.skip_reason))
                    continue

                try:
                    text = task.work()
                except (AuthenticationError, RateLimitError) as e:
                    # Every following page would fail the same way; this page stays missing.
                    result.outcome, result.reason = "stopped", str(e)
                    break
                except BasiraError as e:
                    reason = str(e)
                    page_file.fail_page(task.page, reason)
                    result.failed[task.page] = reason
                    reporter.emit(PageFailed(task.page, reason, time.monotonic() - page_started))
                    consecutive_failures += 1
                    if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                        result.outcome = "stopped"
                        result.reason = f"{consecutive_failures} pages in a row failed: {reason}"
                        break
                    continue

                page_file.write_page(task.page, text)
                result.done.add(task.page)
                result.failed.pop(task.page, None)
                reporter.emit(PageDone(task.page, time.monotonic() - page_started))
                consecutive_failures = 0
    except KeyboardInterrupt:
        result.outcome = "paused"
    finally:
        provider.on_retry = None
        missing = expected_pages - page_file.completed()
        page_file.set_field("processing_status", "Completed" if not missing else "Incomplete")
        result.elapsed = time.monotonic() - started
    return result
