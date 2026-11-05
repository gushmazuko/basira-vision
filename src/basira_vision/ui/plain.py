"""Line-oriented renderers for logs, pipes and CI: logfmt text or JSON lines."""

import json
import sys
from dataclasses import asdict
from datetime import datetime
from typing import Optional, TextIO

from .events import (
    ErrorReported,
    Notice,
    PageDone,
    PageFailed,
    PageRetrying,
    PageStarted,
    RunFinished,
    RunStarted,
    StepDone,
    StepStarted,
    StopRequested,
)
from .state import RunState, format_duration, format_pages


def _quote(value) -> str:
    text = str(value)
    if text == "" or any(c in text for c in ' "=\t\n'):
        return json.dumps(text, ensure_ascii=False)
    return text


class PlainRenderer:
    """``2026-10-27 20:44:31 ocr ok      page=5 progress=5/300 took=2.1s eta=9:58``"""

    def __init__(self, stage: str, stream: Optional[TextIO] = None):
        self.stage = stage
        self.stream = stream
        self.state: Optional[RunState] = None

    def emit(self, event) -> None:
        if isinstance(event, RunStarted):
            self.state = RunState(event)
        elif self.state is not None:
            self.state.apply(event)
        handler = getattr(self, f"_on_{type(event).__name__}", None)
        if handler:
            handler(event)

    def close(self) -> None:
        pass

    def _line(self, kind: str, **fields) -> None:
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        body = " ".join(f"{k}={_quote(v)}" for k, v in fields.items() if v is not None and v != "")
        stream = self.stream or sys.stderr
        print(f"{stamp} {self.stage} {kind:<7} {body}".rstrip(), file=stream, flush=True)

    def _progress(self) -> str:
        return f"{self.state.completed}/{self.state.total}" if self.state and self.state.total else ""

    def _on_RunStarted(self, event: RunStarted) -> None:
        self._line("start", input=event.source, output=event.output, pages=event.total,
                   todo=len(event.todo) if event.pages is not None else None,
                   done_before=len(event.done_before) or None, info=event.details)

    def _on_PageStarted(self, event: PageStarted) -> None:
        pass

    def _on_PageRetrying(self, event: PageRetrying) -> None:
        self._line("wait" if event.rate_limited else "retry", page=event.page,
                   attempt=f"{event.attempt}/{event.attempts}", wait=f"{event.wait:.0f}s", reason=event.reason)

    def _on_PageDone(self, event: PageDone) -> None:
        eta = self.state.eta() if self.state else None
        self._line("ok", page=event.page, progress=self._progress(), took=f"{event.seconds:.1f}s",
                   eta=format_duration(eta) if eta is not None else None)

    def _on_PageFailed(self, event: PageFailed) -> None:
        self._line("failed", page=event.page, progress=self._progress(), reason=event.reason)

    def _on_StopRequested(self, event: StopRequested) -> None:
        self._line("stop", msg="stopping after the current page", page=event.page)

    def _on_StepStarted(self, event: StepStarted) -> None:
        self._line("step", name=event.name)

    def _on_StepDone(self, event: StepDone) -> None:
        self._line("step", name=event.name, took=f"{event.seconds:.1f}s")

    def _on_Notice(self, event: Notice) -> None:
        self._line(event.level, msg=event.message, hint=event.hint)

    def _on_RunFinished(self, event: RunFinished) -> None:
        pages = f"{event.done}/{event.total}" if event.total is not None else None
        self._line(event.outcome, pages=pages, failed=format_pages(event.failed) or None,
                   took=format_duration(event.elapsed), retries=event.retries or None,
                   reason=event.reason, info=event.extra)
        for hint in event.hints:
            self._line("hint", label=hint.label, text=hint.text, note=hint.note)

    def _on_ErrorReported(self, event: ErrorReported) -> None:
        self._line("error", msg=event.message)
        for hint in event.hints:
            self._line("hint", label=hint.label, text=hint.text, note=hint.note)


class JsonRenderer:
    """One JSON object per event: ``{"time": ..., "stage": ..., "event": "page_done", ...}``."""

    def __init__(self, stage: str, stream: Optional[TextIO] = None):
        self.stage = stage
        self.stream = stream

    def emit(self, event) -> None:
        name = "".join(f"_{c.lower()}" if c.isupper() else c for c in type(event).__name__).lstrip("_")
        record = {"time": datetime.now().isoformat(timespec="seconds"), "stage": self.stage, "event": name}
        payload = asdict(event)
        if "failed" in payload:
            payload["failed"] = {str(k): v for k, v in payload["failed"].items()}
        record.update(payload)
        print(json.dumps(record, ensure_ascii=False), file=self.stream or sys.stderr, flush=True)

    def close(self) -> None:
        pass
