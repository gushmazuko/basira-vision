"""Events the pipeline emits. Renderers decide how (and whether) to show them."""

from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class RunStarted:
    """A command starts its work. ``pages`` (every page number of the book) is None for typst."""

    stage: str
    source: str
    output: str
    details: str
    pages: Optional[List[int]] = None
    todo: List[int] = field(default_factory=list)
    done_before: List[int] = field(default_factory=list)

    @property
    def total(self) -> Optional[int]:
        return None if self.pages is None else len(self.pages)


@dataclass
class PageStarted:
    page: int
    activity: str = ""  # e.g. "translating · context: page 6"


@dataclass
class PageRetrying:
    """The provider waits before retrying the current page."""

    page: int
    reason: str
    attempt: int
    attempts: int
    wait: float
    rate_limited: bool = False


@dataclass
class PageDone:
    page: int
    seconds: float


@dataclass
class PageFailed:
    page: int
    reason: str
    seconds: float = 0.0


@dataclass
class StopRequested:
    """First Ctrl-C: the run stops after the page in progress."""

    page: Optional[int]


@dataclass
class StepStarted:
    name: str


@dataclass
class StepDone:
    name: str
    seconds: float


@dataclass
class Notice:
    """A message that is not tied to a page. level: warning, info or debug."""

    level: str
    message: str
    hint: str = ""


@dataclass
class Hint:
    """A labelled line in a summary or error, e.g. ``retry  basira ocr ... --resume``."""

    label: str
    text: str
    command: bool = False
    note: str = ""


@dataclass
class RunFinished:
    """
    outcome: done (possibly with failed pages), paused (Ctrl-C), stopped
    (the run could not go on: endpoint down, key rejected, rate limit).
    """

    stage: str
    outcome: str
    reason: Optional[str] = None
    done: int = 0
    total: Optional[int] = None
    failed: Dict[int, str] = field(default_factory=dict)
    elapsed: float = 0.0
    retries: int = 0
    extra: str = ""
    hints: List[Hint] = field(default_factory=list)


@dataclass
class ErrorReported:
    """A command could not start or failed outside the page loop."""

    message: str
    hints: List[Hint] = field(default_factory=list)
