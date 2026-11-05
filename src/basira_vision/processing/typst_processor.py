"""Book PDF generation with Pandoc and the Typst template."""

import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from ..utils.constants import LANGUAGE_CODES
from ..utils.errors import BasiraError
from ..utils.logging import get_logger
from ..utils.pages import split_frontmatter

logger = get_logger("typst")

TEMPLATE_PATH = Path(__file__).resolve().parent.parent / "templates" / "template.typ"

# Template variables that are passed straight through when set. Layout
# (page size, margins, fonts, spacing) lives only in the template.
TEMPLATE_VARIABLES = ("subtitle", "version", "date", "lang", "header", "toc_title")


class TypesettingError(BasiraError):
    """Pandoc or Typst failed, or required input is missing."""


def missing_tools() -> List[str]:
    """Names of the external programs that are not on PATH."""
    return [tool for tool in ("pandoc", "typst") if shutil.which(tool) is None]


def resolve_metadata(
    markdown_content: str,
    title: Optional[str] = None,
    author: Optional[str] = None,
    translator_preface: Optional[str] = None,
    lang: Optional[str] = None,
    base_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Merge YAML frontmatter with CLI overrides (CLI wins).

    The document language falls back to the frontmatter's
    ``target_language`` (a translation) or ``source_language`` (an OCR file).
    A relative ``translator_preface`` path in the frontmatter is taken
    relative to ``base_dir``, the folder of the markdown file.
    """
    meta, _, _ = split_frontmatter(markdown_content)

    authors = meta.get("author") or meta.get("authors") or []
    if isinstance(authors, str):
        authors = [authors]
    authors = [str(a).strip() for a in authors if a and str(a).strip()]
    if author and author.strip():
        authors = [author.strip()]

    if not lang:
        language = str(meta.get("target_language") or meta.get("source_language") or "").lower()
        lang = LANGUAGE_CODES.get(language)

    return {
        "title": (title or str(meta.get("title") or "")).strip(),
        "authors": authors,
        "translator_preface": translator_preface or _frontmatter_path(meta.get("translator_preface"), base_dir),
        "lang": lang,
    }


def _frontmatter_path(value: Any, base_dir: Optional[Path]) -> Optional[str]:
    if not value:
        return None
    path = Path(str(value)).expanduser()
    return str(base_dir / path if base_dir is not None else path)


def _convert_preface(preface_path: Path, output_dir: Path) -> Path:
    """Convert the translator's preface from markdown to a Typst file next to the output."""
    preface_typ = output_dir / f".{preface_path.stem}.preface.typ"
    _run(["pandoc", str(preface_path), "-t", "typst", "-o", str(preface_typ), "--wrap=none"])
    # The preface is included as a separate file, so the template's scene-break
    # definitions are not visible there; draw a plain rule instead.
    content = preface_typ.read_text(encoding="utf-8")
    for rule in ("#horizontalrule", "#divider()"):
        content = content.replace(rule, "#line(length: 100%)")
    preface_typ.write_text(content, encoding="utf-8")
    return preface_typ


class _step:
    """Report a named step to on_step before and after it runs."""

    def __init__(self, on_step, name: str):
        self.on_step, self.name = on_step, name

    def __enter__(self):
        self.started = time.monotonic()
        if self.on_step:
            self.on_step(self.name, None)

    def __exit__(self, exc_type, *exc):
        if self.on_step and exc_type is None:
            self.on_step(self.name, time.monotonic() - self.started)


def _run(command: List[str], cwd: Optional[Path] = None) -> None:
    logger.debug(f"Running: {' '.join(command)}")
    result = subprocess.run(command, cwd=cwd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        details = (result.stderr or result.stdout).strip()
        raise TypesettingError(f"{command[0]} failed (exit {result.returncode}): {details}")


def generate_pdf(
    markdown_path: Path,
    output_path: Path,
    title: Optional[str] = None,
    author: Optional[str] = None,
    translator_preface: Optional[str] = None,
    toc: Optional[bool] = None,
    template_path: Path = TEMPLATE_PATH,
    on_step: Optional[Callable[[str, Optional[float]], None]] = None,
    **variables: Any,
) -> None:
    """
    Typeset a markdown file into a book PDF.

    Args:
        markdown_path: Source markdown (YAML frontmatter supplies defaults)
        output_path: PDF to write
        title, author: Override the frontmatter
        translator_preface: Markdown file inserted after the title page
        toc: Force the table of contents on or off
        on_step: Called as on_step(name, None) when a step starts and
            on_step(name, seconds) when it is done
        **variables: Values for TEMPLATE_VARIABLES; None means "template default"

    Raises:
        TypesettingError: On missing title/author/preface or a Pandoc/Typst failure
    """
    metadata = resolve_metadata(
        markdown_path.read_text(encoding="utf-8"), title, author, translator_preface, variables.get("lang"),
        base_dir=markdown_path.parent,
    )
    missing = [name for name in ("title", "authors") if not metadata[name]]
    if missing:
        raise TypesettingError(
            f"No {' and '.join(m.rstrip('s') for m in missing)} in the frontmatter of {markdown_path}; "
            "add title:/author: there or pass --title/--author"
        )
    variables["lang"] = metadata["lang"]

    output_path = output_path.resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    command = [
        "pandoc", str(markdown_path.resolve()),
        "-o", str(output_path),
        "--pdf-engine=typst",
        f"--template={Path(template_path).expanduser().resolve()}",
        "-V", f"title={metadata['title']}",
        "-V", f"author={metadata['authors'][0]}",
    ]
    for name in TEMPLATE_VARIABLES:
        value = variables.get(name)
        if value is not None and value != "":
            command += ["-V", f"{name}={value}"]
    if toc is not None:
        command += ["-V", f"show_toc={'true' if toc else 'false'}"]

    preface_typ = None
    try:
        if metadata["translator_preface"]:
            preface = Path(metadata["translator_preface"]).expanduser().resolve()
            if not preface.is_file():
                raise TypesettingError(f"Translator preface not found: {preface}")
            with _step(on_step, "converting the translator's preface"):
                preface_typ = _convert_preface(preface, output_path.parent)
            # The template includes the preface by a path relative to the working directory.
            command += ["-V", f"translator_preface={preface_typ.name}"]

        with _step(on_step, "typesetting with Pandoc and Typst"):
            _run(command, cwd=output_path.parent)
    finally:
        if preface_typ is not None:
            preface_typ.unlink(missing_ok=True)

    logger.debug(f"Wrote {output_path}")
