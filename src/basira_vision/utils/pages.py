"""Page files: markdown with YAML frontmatter and one ``<!-- Page N -->`` block per page.

Both the OCR output and the translation are page files. Every page carries
its original page number, so a run can stop anywhere and resume, process
pages in any order (``--page-range``), and retry pages that failed.

A failed page keeps its reason inside the marker and has no body::

    <!-- Page 9 - Error: HTTP 500 INTERNAL -->

Markers are HTML comments, which Pandoc drops, so a failure never ends up
in the typeset book. Writing text under an error marker by hand counts as
fixing the page: it is treated as done from then on.
"""

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import yaml

from .constants import SeparatorType
from .logging import get_logger

logger = get_logger("pages")

_FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---[ \t]*(?:\n|\Z)", re.S)
_MARKER_RE = re.compile(r"^<!-- Page (\d+)(?P<error> - Error(?::[ \t]*(?P<reason>.*?))?)?[ \t]*-->[ \t]*\n?", re.M)
_TYPST_PAGEBREAK = "```{=typst}\n#pagebreak()\n```"
_SEPARATORS = {
    SeparatorType.OCR: "\n\n---\n\n",
    SeparatorType.TYPST: f"\n{_TYPST_PAGEBREAK}\n\n",
}
_FILL_IN = "  # fill in, or pass --{key} to the typst command"


@dataclass
class Page:
    number: int
    text: str
    error: Optional[str] = None

    @property
    def is_error(self) -> bool:
        return self.error is not None


def split_frontmatter(content: str) -> Tuple[Dict[str, Any], str, str]:
    """Split a document into (parsed frontmatter, raw frontmatter text, body)."""
    match = _FRONTMATTER_RE.match(content)
    if not match:
        return {}, "", content
    raw = match.group(1)
    try:
        meta = yaml.safe_load(raw) or {}
    except yaml.YAMLError as e:
        logger.warning(f"Could not parse YAML frontmatter: {e}")
        meta = {}
    if not isinstance(meta, dict):
        meta = {}
    return meta, raw, content[match.end():]


def _strip_separator(text: str) -> str:
    """Remove the page separator that trails a page block."""
    text = text.rstrip()
    if text.endswith(_TYPST_PAGEBREAK):
        text = text[: -len(_TYPST_PAGEBREAK)]
    else:
        lines = text.split("\n")
        if lines and re.fullmatch(r"-{3,}", lines[-1].strip()):
            text = "\n".join(lines[:-1])
    return text.strip()


def _clean_reason(reason: str) -> str:
    """Make a failure reason safe to put inside an HTML comment on one line."""
    reason = " ".join(reason.split())
    return reason.replace("--", "—") or "unknown error"


def parse_pages(body: str) -> List[Page]:
    """Parse page blocks from a document body (frontmatter already removed).

    A body without page markers is treated as a single page 1.
    """
    markers = list(_MARKER_RE.finditer(body))
    if not markers:
        text = body.strip()
        return [Page(1, text)] if text else []

    pages: Dict[int, Page] = {}
    for i, marker in enumerate(markers):
        end = markers[i + 1].start() if i + 1 < len(markers) else len(body)
        number = int(marker.group(1))
        text = _strip_separator(body[marker.end():end])
        error = None
        if marker.group("error") and not text:
            error = (marker.group("reason") or "").strip() or "unknown error"
        pages[number] = Page(number, text, error)
    return [pages[n] for n in sorted(pages)]


def read_page_document(content: str) -> Tuple[Dict[str, Any], List[Page]]:
    """Parse a page file into its frontmatter and pages."""
    meta, _, body = split_frontmatter(content)
    return meta, parse_pages(body)


def _yaml_value(value: Any) -> str:
    # JSON strings are valid YAML double-quoted scalars, so quotes and
    # non-ASCII text survive without a YAML emitter.
    return "null" if value is None else json.dumps(value, ensure_ascii=False)


class PageFile:
    """A page file on disk that is rewritten atomically after every page."""

    def __init__(
        self,
        path: Path,
        separator: SeparatorType,
        frontmatter: Dict[str, Any],
        resume: bool = False,
    ):
        """
        Open or create a page file.

        Args:
            path: Markdown file to write
            separator: Page separator style (OCR thematic break or Typst page break)
            frontmatter: Fields for a new file; ignored when resuming an existing one
            resume: Keep the pages of an existing file instead of starting over
        """
        self.path = path
        self.separator = separator
        self.pages: Dict[int, Page] = {}

        if resume and path.exists():
            _, self._frontmatter, body = split_frontmatter(path.read_text(encoding="utf-8"))
            self.pages = {page.number: page for page in parse_pages(body)}
            logger.debug(f"Resuming {path}: {len(self.completed())} pages done")
        else:
            lines = []
            for key, value in frontmatter.items():
                line = f"{key}: {_yaml_value(value)}"
                if key in ("title", "author") and not value:
                    line += _FILL_IN.format(key=key)
                lines.append(line)
            self._frontmatter = "\n".join(lines)
            path.parent.mkdir(parents=True, exist_ok=True)
            self._save()

    def completed(self) -> Set[int]:
        """Numbers of pages written without an error."""
        return {n for n, page in self.pages.items() if not page.is_error}

    def failed(self) -> Dict[int, str]:
        """Failed pages and their reasons."""
        return {n: page.error for n, page in self.pages.items() if page.is_error}

    def get(self, number: int) -> Optional[Page]:
        return self.pages.get(number)

    def write_page(self, number: int, text: str) -> None:
        """Add or replace a finished page and save the file."""
        self.pages[number] = Page(number, text.strip())
        self._save()

    def fail_page(self, number: int, reason: str) -> None:
        """Record a failed page (reason in the marker, no body) and save the file."""
        self.pages[number] = Page(number, "", _clean_reason(reason))
        self._save()

    def set_field(self, key: str, value: Any) -> None:
        """Set a frontmatter field (only the frontmatter is touched) and save."""
        line = f"{key}: {_yaml_value(value)}"
        pattern = re.compile(rf"^{re.escape(key)}:.*$", re.M)
        if pattern.search(self._frontmatter):
            self._frontmatter = pattern.sub(lambda _: line, self._frontmatter, count=1)
        else:
            self._frontmatter = f"{self._frontmatter}\n{line}" if self._frontmatter else line
        self._save()

    def render(self) -> str:
        blocks = []
        for number in sorted(self.pages):
            page = self.pages[number]
            if page.is_error:
                blocks.append(f"<!-- Page {number} - Error: {page.error} -->")
            else:
                blocks.append(f"<!-- Page {number} -->\n{page.text}".rstrip())
        body = _SEPARATORS[self.separator].join(blocks)
        return f"---\n{self._frontmatter}\n---\n\n{body}\n" if body else f"---\n{self._frontmatter}\n---\n"

    def _save(self) -> None:
        tmp = self.path.with_name(f".{self.path.name}.tmp")
        tmp.write_text(self.render(), encoding="utf-8")
        os.replace(tmp, self.path)


def describe_existing(path: Path, total: Optional[int] = None) -> Tuple[int, Dict[int, str], int]:
    """Summarize an existing page file: (pages done, failed pages, total if known)."""
    _, pages = read_page_document(path.read_text(encoding="utf-8"))
    done = sum(1 for p in pages if not p.is_error)
    failed = {p.number: p.error for p in pages if p.is_error}
    return done, failed, total if total is not None else len(pages)
