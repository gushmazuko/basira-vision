"""Terminal output.

The pipeline never prints. It emits events (``ui.events``) to a Reporter,
which hands them to one renderer:

- live: an interactive view for terminals (pinned progress, events above it)
- plain: one logfmt line per event, for pipes, ``nohup`` and CI
- json: one JSON object per event, for scripts

Everything goes to stderr; stdout is reserved for the result path.
"""

import os
import sys
from pathlib import Path
from typing import Iterable, Optional

from ..utils.logging import route_logging
from .events import ErrorReported, Hint, Notice

LOG_FORMATS = ("auto", "live", "plain", "json")


def _interactive() -> bool:
    return sys.stderr.isatty() and os.environ.get("TERM") != "dumb"


class Reporter:
    def __init__(self, stage: str, log_format: str = "auto", verbose: bool = False):
        self.stage = stage
        self.verbose = verbose
        if log_format == "auto":
            log_format = "live" if _interactive() else "plain"
        self.format = log_format
        if log_format == "live":
            from .live import LiveRenderer

            self.renderer = LiveRenderer()
        elif log_format == "json":
            from .plain import JsonRenderer

            self.renderer = JsonRenderer(stage)
        else:
            from .plain import PlainRenderer

            self.renderer = PlainRenderer(stage)
        self._logging = None

    def __enter__(self) -> "Reporter":
        self._logging = route_logging(lambda level, message: self.emit(Notice(level, message)), self.verbose)
        self._logging.__enter__()
        return self

    def __exit__(self, *exc) -> None:
        try:
            self.renderer.close()
        finally:
            self._logging.__exit__(*exc)

    def emit(self, event) -> None:
        self.renderer.emit(event)

    def warn(self, message: str, hint: str = "") -> None:
        self.emit(Notice("warning", message, hint))

    def error(self, message: str, hints: Iterable[Hint] = ()) -> None:
        self.emit(ErrorReported(message, list(hints)))


def display_path(path: Path, base: Optional[Path] = None) -> str:
    """A path as short as possible: relative to the working directory, else ~-abbreviated."""
    path = Path(path).expanduser()
    absolute = path if path.is_absolute() else (base or Path.cwd()) / path
    try:
        return str(absolute.resolve().relative_to((base or Path.cwd()).resolve()))
    except ValueError:
        pass
    try:
        return "~/" + str(absolute.resolve().relative_to(Path.home().resolve()))
    except ValueError:
        return str(absolute)


def print_result_path(path: Path) -> None:
    """Write the result path to stdout when it is piped, so commands can be chained."""
    if not sys.stdout.isatty():
        print(display_path(path), flush=True)


__all__ = ["Hint", "LOG_FORMATS", "Reporter", "display_path", "print_result_path"]
