"""Terminal output.

The pipeline never prints. It emits events (``ui.events``) to a Reporter,
which hands them to one renderer:

- live: an interactive view for terminals (pinned progress, events above it)
- plain: one logfmt line per event, for pipes, ``nohup`` and CI
- json: one JSON object per event, for scripts

Everything goes to stderr; stdout is reserved for the result path.

Next to that, a run keeps a log file (``journal.py``): the same events as plain lines,
appended to ``<output>.log``, whatever the terminal shows.
"""

import logging
import os
import sys
import threading
from pathlib import Path
from typing import Iterable, Optional

from ..utils.logging import route_logging
from .events import ErrorReported, Hint, Notice
from .journal import Journal

LOG_FORMATS = ("auto", "live", "plain", "json")


def _interactive() -> bool:
    return sys.stderr.isatty() and os.environ.get("TERM") != "dumb"


class Reporter:
    """Where a command reports to: one renderer for the terminal and, if kept, the run's log file."""

    def __init__(self, stage: str, log_format: str = "auto", verbose: bool = False,
                 journal: Optional[Journal] = None):
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
        self._journal = journal
        if journal is not None:
            journal.on_problem = self._log_problem
        self._logging = None
        # capture reports from several threads; a renderer handles one event at a time.
        self._lock = threading.RLock()

    def __enter__(self) -> "Reporter":
        # The log keeps info records; the terminal shows anything below a warning only with --verbose.
        level = logging.DEBUG if self.verbose else logging.INFO if self._journal is not None else logging.WARNING
        self._logging = route_logging(self._record, level)
        self._logging.__enter__()
        return self

    def __exit__(self, *exc) -> None:
        # Records stop coming first: a thread that is still running must not write to a closed log.
        try:
            self._logging.__exit__(*exc)
        finally:
            with self._lock:
                journal, self._journal = self._journal, None
                try:
                    if journal is not None:
                        journal.close()
                finally:
                    self.renderer.close()

    def emit(self, event) -> None:
        with self._lock:
            self.renderer.emit(event)
            if self._journal is not None:
                self._journal.emit(event)

    def _record(self, level: str, message: str) -> None:
        """A record of the package's logging (or a third-party warning)."""
        with self._lock:
            if level == "warning":
                self.renderer.emit(Notice("warning", message))
            elif self.verbose:  # on the terminal everything below a warning is a detail
                self.renderer.emit(Notice("debug", message))
            if self._journal is not None and (level != "debug" or self.verbose):
                self._journal.emit(Notice(level, message))

    def warn(self, message: str, hint: str = "") -> None:
        self.emit(Notice("warning", message, hint))

    def error(self, message: str, hints: Iterable[Hint] = ()) -> None:
        self.emit(ErrorReported(message, list(hints)))

    # ------------------------------------------------------------------ the log file

    def log_to(self, output: Path) -> None:
        """Keep the log of this run next to the file the command writes, as ``<name>.log``.

        If that file or its log is there already, the log is written from now on, so a run that
        is refused is in it too. If not, nothing is written until ``start_log``: a first run
        that fails before it creates anything leaves no log behind either.
        """
        if self._journal is None:
            return
        log = output.with_suffix(".log")
        if log == output:  # the output itself is called something.log
            log = output.with_name(output.name + ".log")
        with self._lock:
            self._journal.attach(log, at_once=output.exists() or log.exists())

    def start_log(self) -> None:
        """The command has created its output: write the log from here on, and what was kept so far."""
        with self._lock:
            if self._journal is not None:
                self._journal.start()

    def crashed(self, error: BaseException) -> None:
        """An error nobody planned for ends the run; the terminal gets the traceback, the log this line."""
        with self._lock:
            if self._journal is not None:
                message = f"stopped by an unexpected error: {type(error).__name__}: {error}"
                self._journal.emit(ErrorReported(message))

    def _log_problem(self, path: Path, error: OSError) -> None:
        # Straight to the terminal: not through the log that just failed, and not through logging.
        reason = error.strerror or str(error)
        self.renderer.emit(Notice("warning", f"the log cannot be written ({display_path(path)}: {reason}); "
                                             "the run goes on without it"))


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


__all__ = ["Hint", "Journal", "LOG_FORMATS", "Reporter", "display_path", "print_result_path"]
