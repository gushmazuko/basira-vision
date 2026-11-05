"""Logging that ends up in the run's output instead of fighting it.

Modules log through ``get_logger``. While a command runs, ``route_logging``
sends those records (and warnings from third-party libraries) to the active
reporter, so they appear in the same place and format as everything else.
"""

import logging
from contextlib import contextmanager
from typing import Callable, Iterator

LOGGER_NAME = "basira_vision"

Sink = Callable[[str, str], None]  # (level, message)


def get_logger(component: str) -> logging.Logger:
    """Return a child logger of the package logger, e.g. ``basira_vision.ocr``."""
    return logging.getLogger(f"{LOGGER_NAME}.{component}")


class _SinkHandler(logging.Handler):
    def __init__(self, sink: Sink):
        super().__init__(logging.DEBUG)
        self.sink = sink
        self._busy = False

    def emit(self, record: logging.LogRecord) -> None:
        if self._busy:  # a renderer that logs must not recurse
            return
        self._busy = True
        try:
            if record.levelno >= logging.WARNING:
                level = "warning"
            elif record.levelno >= logging.INFO:
                level = "info"
            else:
                level = "debug"
            self.sink(level, record.getMessage())
        except Exception:
            self.handleError(record)
        finally:
            self._busy = False


@contextmanager
def route_logging(sink: Sink, verbose: bool = False) -> Iterator[None]:
    """Send package logs (debug with --verbose) and third-party warnings to ``sink``."""
    root = logging.getLogger()
    package = logging.getLogger(LOGGER_NAME)
    saved = (root.level, package.level, package.propagate)
    handler = _SinkHandler(sink)

    root.addHandler(handler)
    root.setLevel(logging.WARNING)
    package.setLevel(logging.DEBUG if verbose else logging.WARNING)
    package.propagate = True
    try:
        yield
    finally:
        root.removeHandler(handler)
        root.setLevel(saved[0])
        package.setLevel(saved[1])
        package.propagate = saved[2]
