"""The log file of a run: what happened, as plain lines, next to the file the command writes.

The terminal shows a run while it lasts; the journal is what is left of it afterwards. It gets
every event the renderers get, formatted by the plain renderer, whatever the terminal shows.

Lines are kept in memory until the command has an output to keep them next to (``attach``,
then ``start``), so a run that fails before it writes anything leaves no log behind either,
and what happened before the file was opened is still in it, with its own times.

Each line goes to the file in one ``os.write`` on a descriptor opened for appending. That
makes a line atomic (two commands on one book do not cut each other's lines), puts it on disk
at once (it survives a killed process), and has no buffer to corrupt when a line is written
from inside another one: Ctrl-C is handled on the main thread, wherever that thread happens
to be, and reports that it was pressed.
"""

import os
from dataclasses import replace
from pathlib import Path
from typing import Callable, List, Optional
from urllib.parse import urlsplit

from .events import CaptureStarted
from .plain import PlainRenderer


class _Lines(PlainRenderer):
    """The plain renderer, with the journal as its output."""

    def __init__(self, stage: str, write: Callable[[str], None]):
        super().__init__(stage)
        self._write = write

    def _out(self, text: str) -> None:
        self._write(text)

    def _on_CaptureStarted(self, event: CaptureStarted) -> None:
        # The key in the address is what lets a device into the running session: the terminal
        # has to show it, a file has no use for it.
        parts = urlsplit(event.url)
        super()._on_CaptureStarted(replace(event, url=f"{parts.scheme}://{parts.netloc}/<key>/"))


class Journal:
    def __init__(self, stage: str, basira: str, command: str,
                 on_problem: Optional[Callable[[Path, OSError], None]] = None):
        """
        Args:
            stage: The command, the second word of every line
            basira: The version, for the line that opens the run
            command: The command as it was typed, for the same line
            on_problem: Called once, with the path and the error, when the file cannot be written
        """
        self.path: Optional[Path] = None
        self.on_problem = on_problem
        self._fd: Optional[int] = None
        # The lines of the run so far while there is no file yet; None once they are written or given up.
        self._kept: Optional[List[str]] = []
        self._lines = _Lines(stage, self._write)
        self._lines._line("run", basira=basira, pid=os.getpid(), command=command)

    def emit(self, event) -> None:
        self._lines.emit(event)

    def attach(self, path: Path, at_once: bool) -> None:
        """Name the file. ``at_once``: it belongs to something that exists; else wait for ``start``."""
        self.path = path
        if at_once:
            self.start()

    def start(self) -> None:
        """Open the file and write what was kept. The folder is the command's to create, not the log's."""
        if self.path is None or self._fd is not None or self._kept is None:
            return
        try:
            flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0)
            self._fd = os.open(self.path, flags, 0o644)
        except OSError as e:
            self._give_up(e)
            return
        kept, self._kept = self._kept, None
        for line in kept:
            self._write(line)

    def close(self) -> None:
        fd, self._fd, self._kept = self._fd, None, None
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass

    def _write(self, text: str) -> None:
        if self._fd is None:
            if self._kept is not None:
                self._kept.append(text)
            return
        try:
            os.write(self._fd, (text + "\n").encode("utf-8", "replace"))
        except OSError as e:
            self._give_up(e)

    def _give_up(self, error: OSError) -> None:
        """A log must never stop the run it is the log of: say it once and go on without."""
        self.close()
        if self.on_problem is not None:
            self.on_problem(self.path, error)
