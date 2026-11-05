"""Command-line interface: ``basira ocr | translate | typst``."""

import functools
import shlex
import shutil
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Iterable, List, Optional

import click
import pymupdf

from . import __version__
from .pipeline import PageTask, RunResult, run_pages
from .processing import typst_processor
from .processing.pdf_converter import IMAGE_FORMATS, PDFRenderer, page_count, parse_page_range
from .processing.prompt_loader import BUILTIN_PROMPTS_DIR, PromptLoader, read_custom_prompt
from .processing.translation import (
    check_translation_template,
    default_output_path,
    previous_page_context,
    render_prompt,
)
from .providers import VisionProvider, create_provider
from .settings import Settings
from .ui import LOG_FORMATS, Hint, Reporter, display_path, print_result_path
from .ui.events import RunFinished, RunStarted, StepDone, StepStarted
from .ui.state import pages_label
from .utils.constants import DEFAULT_TARGET_LANGUAGE, SeparatorType
from .utils.errors import AuthenticationError, BasiraError, ConfigurationError, EndpointUnreachableError
from .utils.pages import PageFile, describe_existing, read_page_document

PROVIDER_NAME = "Gemini"


class CommandError(BasiraError):
    """An error shown to the user together with what to do about it."""

    def __init__(self, message: str, hints: Iterable[Hint] = ()):
        super().__init__(message)
        self.hints = list(hints)


class Placeholder(str):
    """A part of a suggested command the user fills in, e.g. ``<language>``; never quoted."""


def command_line(*args) -> str:
    """A copy-pasteable ``basira ...`` command; paths are shortened, None arguments dropped."""
    parts = ["basira"]
    for arg in args:
        if arg is None:
            continue
        if isinstance(arg, Placeholder):
            parts.append(arg)
        else:
            parts.append(shlex.quote(display_path(arg) if isinstance(arg, Path) else str(arg)))
    return " ".join(parts)


def _option(flag: str, value, default=None) -> list:
    return [flag, value] if value is not None and value != default else []


def _command(stage: str):
    """Shared options, the reporter's lifetime, error display and the exit code."""

    def decorate(func):
        @click.option("--log-format", type=click.Choice(LOG_FORMATS), default="auto", show_default=True,
                      help="auto: live view in a terminal, plain lines otherwise; json: one event per line")
        @click.option("--verbose", "-v", is_flag=True, help="Show debug details")
        @functools.wraps(func)
        def wrapper(log_format, verbose, **kwargs):
            with Reporter(stage, log_format, verbose) as reporter:
                try:
                    code = func(reporter, **kwargs)
                except CommandError as e:
                    reporter.error(str(e), e.hints)
                    code = 1
                except BasiraError as e:
                    reporter.error(str(e))
                    code = 1
                except KeyboardInterrupt:
                    reporter.error("interrupted before any page was processed")
                    code = 130
            sys.exit(code or 0)

        return wrapper

    return decorate


def _check_mode(resume: bool, force_restart: bool) -> None:
    if resume and force_restart:
        raise click.UsageError("--resume and --force-restart cannot be used together")


def _refuse_overwrite(path: Path, total: Optional[int], resume: bool, force_restart: bool, rerun: List) -> None:
    """Never replace finished work silently: ask for --resume or --force-restart."""
    if not path.exists() or resume or force_restart:
        return
    done, failed, total = describe_existing(path, total)
    state = f"{done} of {total} pages done" + (f", {len(failed)} failed" if failed else "")
    keep = "keeps it and your edits"
    if failed:
        keep += f", retries {pages_label(failed)}"
    raise CommandError(
        f"{display_path(path)} already exists ({state}). Nothing was changed.",
        [
            Hint("continue", command_line(*rerun, "--resume"), command=True, note=keep),
            Hint("restart", command_line(*rerun, "--force-restart"), command=True, note="deletes it and starts over"),
        ],
    )


def _connect(settings: Settings, model: Optional[str], reporter: Reporter) -> VisionProvider:
    if not settings.gemini_api_key:
        raise CommandError("GEMINI_API_KEY is not set", [
            Hint("set it", "export GEMINI_API_KEY=your_key", note="or put it in a .env file in this directory"),
            Hint("get one", "https://aistudio.google.com/"),
        ])
    try:
        provider = create_provider(
            api_key=settings.gemini_api_key,
            model_name=model or settings.gemini_model,
            base_url=settings.gemini_base_url or None,
        )
    except ValueError as e:
        raise ConfigurationError(str(e)) from e

    try:
        warning = provider.check_connection()
    except EndpointUnreachableError as e:
        if settings.gemini_base_url:
            hint = Hint("check", f"is the gateway running?  curl {settings.gemini_base_url.rstrip('/')}/v1beta/models")
        else:
            hint = Hint("check", "your network connection")
        raise CommandError(f"cannot reach the model endpoint: {e}", [hint]) from None
    except AuthenticationError as e:
        raise CommandError(str(e), [Hint("check", "GEMINI_API_KEY (environment or .env)")]) from None
    if warning:
        reporter.warn(warning)
    return provider


def _model_details(settings: Settings, model: Optional[str], provider: VisionProvider) -> str:
    details = model or settings.gemini_model
    if settings.gemini_base_url:
        details += f" via {provider.endpoint}"
    return details


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _finish(reporter: Reporter, stage: str, result: RunResult, page_file: PageFile, total: int,
            hints: List[Hint]) -> int:
    reporter.emit(RunFinished(
        stage=stage,
        outcome=result.outcome,
        reason=result.reason,
        done=len(page_file.completed()),
        total=total,
        failed=result.failed,
        elapsed=result.elapsed,
        retries=result.retries,
        hints=hints,
    ))
    if result.outcome == "done":
        print_result_path(page_file.path)
    return result.exit_code()


def _resume_hints(result: RunResult, rerun: List) -> List[Hint]:
    resume = command_line(*rerun, "--resume")
    if result.outcome == "paused":
        return [Hint("continue", resume, command=True)]
    if result.outcome == "stopped":
        return [Hint("continue", resume, command=True, note="once the cause is fixed")]
    if result.failed:
        return [Hint("retry", resume, command=True, note="retries only the failed pages")]
    return []


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, prog_name="basira")
def main():
    """
    Basira Vision: scanned book in, a new edition in your language out.

    \b
    1. basira ocr book.pdf --source-language english
    2. review workspace/book/book.md, then:
       basira translate workspace/book/book.md --target-language russian
    3. review workspace/book/book_russian.md, then:
       basira typst workspace/book/book_russian.md --title "..." --author "..."

    Configuration comes from the environment or a .env file:
    GEMINI_API_KEY (required), GEMINI_MODEL, GEMINI_BASE_URL,
    BASIRA_OUTPUT_DIR, BASIRA_PROMPTS_DIR.

    Exit codes: 0 done, 3 done with failed pages, 1 error, 2 bad usage,
    130 paused with Ctrl-C.
    """


# ---------------------------------------------------------------------------- ocr


@main.command()
@click.argument("pdf_file", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--source-language", default="english", show_default=True,
              help="OCR prompt template, named after the language of the scan")
@click.option("--page-range", help="Pages to process, e.g. 5 or 1-10 (default: all)")
@click.option("--dpi", type=click.IntRange(72, 600), default=300, show_default=True,
              help="Render resolution; use 600 for poor scans")
@click.option("--model", "-m", help="Model name (overrides GEMINI_MODEL)")
@click.option("--custom-prompt", help="OCR prompt text, or a path to a prompt file")
@click.option("--prompts-dir", type=click.Path(file_okay=False, path_type=Path),
              help="Extra prompt directory searched before the built-in one (env: BASIRA_PROMPTS_DIR)")
@click.option("--output-dir", type=click.Path(file_okay=False, path_type=Path),
              help="Workspace directory (default: ./workspace, env: BASIRA_OUTPUT_DIR)")
@click.option("--image-format", type=click.Choice(IMAGE_FORMATS, case_sensitive=False), default="PNG",
              show_default=True, help="Format of the rendered page images")
@click.option("--no-images", is_flag=True, help="Do not keep the rendered page images")
@click.option("--resume", is_flag=True, help="Keep finished pages and process only missing or failed ones")
@click.option("--force-restart", is_flag=True, help="Delete the existing output and start over")
@_command("ocr")
def ocr(reporter, pdf_file, source_language, page_range, dpi, model, custom_prompt, prompts_dir,
        output_dir, image_format, no_images, resume, force_restart):
    """Read every page of a scanned PDF with a vision model into markdown.

    Writes <workspace>/<name>/<name>.md with one <!-- Page N --> block per page.
    """
    _check_mode(resume, force_restart)
    settings = Settings()
    source_language = source_language.strip().lower()

    total = page_count(pdf_file)
    try:
        requested = parse_page_range(page_range, total)
    except ValueError as e:
        raise click.BadParameter(str(e), param_hint="--page-range") from None

    if custom_prompt:
        prompt, prompt_label = read_custom_prompt(custom_prompt), "custom"
    else:
        prompt = PromptLoader(prompts_dir or settings.prompts_dir).load("ocr", source_language)
        prompt_label = f"ocr/{source_language}"

    document_dir = settings.workspace_dir(output_dir) / pdf_file.stem
    output_path = document_dir / f"{pdf_file.stem}.md"
    rerun = [
        "ocr", pdf_file,
        *_option("--page-range", page_range),
        *_option("--source-language", source_language, "english"),
        *_option("--custom-prompt", custom_prompt),
        *_option("--prompts-dir", prompts_dir),
        *_option("--output-dir", output_dir),
        *_option("--model", model),
        *_option("--dpi", dpi, 300),
        *_option("--image-format", image_format.upper(), "PNG"),
        *(["--no-images"] if no_images else []),
    ]
    _refuse_overwrite(output_path, total, resume, force_restart, rerun)

    model_name = model or settings.gemini_model
    provider = _connect(settings, model, reporter)
    replacing = force_restart and output_path.exists()
    page_file = PageFile(
        output_path,
        SeparatorType.OCR,
        {
            "title": "",
            "author": "",
            "source_language": source_language,
            "ai_provider": PROVIDER_NAME,
            "ai_model": model_name,
            "generated": _now(),
            "processing_status": "In progress",
        },
        resume=resume,
    )
    completed = page_file.completed()
    todo = [n for n in requested if n not in completed]

    details = [f"{total} pages"]
    if page_range:
        details.append(f"range {page_range}")
    if completed:
        details.append(f"{len(completed)} done earlier")
    details += [f"prompt {prompt_label}", _model_details(settings, model, provider), f"{dpi} dpi"]
    reporter.emit(RunStarted("ocr", display_path(pdf_file), display_path(output_path), " · ".join(details),
                             pages=list(range(1, total + 1)), todo=todo, done_before=sorted(completed)))
    if replacing:
        reporter.warn("starting over: the previous output was deleted")
    if not todo:
        reporter.warn("nothing to do: every requested page is already done")

    images_dir = None if no_images else document_dir / "images"
    with tempfile.TemporaryDirectory(prefix="basira_") as tmp, PDFRenderer(pdf_file, dpi, image_format) as renderer:
        target_dir = images_dir or Path(tmp)

        def read_page(number: int):
            return lambda: provider.extract_text(renderer.render(number, target_dir), prompt)

        tasks = [PageTask(n, read_page(n), "reading") for n in todo]
        result = run_pages(tasks, page_file, provider, reporter, set(range(1, total + 1)))

    hints = _resume_hints(result, rerun)
    outside = set(range(1, total + 1)) - page_file.completed() - set(page_file.failed()) - set(requested)
    if outside and result.outcome == "done":
        first, last = min(outside), max(outside)
        if last - first + 1 == len(outside):
            without_range = [arg for i, arg in enumerate(rerun)
                             if arg != "--page-range" and (i == 0 or rerun[i - 1] != "--page-range")]
            span = str(first) if first == last else f"{first}-{last}"
            hints.append(Hint("remaining", command_line(*without_range, "--page-range", span, "--resume"),
                              command=True, note=f"{pages_label(outside)} not processed yet"))
        else:
            hints.append(Hint("remaining", f"{pages_label(outside)} not processed yet",
                              note="process them with --page-range … --resume"))
    hints.append(Hint("output", display_path(output_path)))
    if images_dir:
        hints.append(Hint("images", display_path(images_dir)))
    if result.outcome == "done" and not page_file.failed() and not outside:
        next_step = command_line("translate", output_path, "--target-language", Placeholder("<language>"))
        hints.append(Hint("next", next_step, command=True, note="after reviewing the markdown"))
    return _finish(reporter, "ocr", result, page_file, total, hints)


# ---------------------------------------------------------------------------- translate


@main.command()
@click.argument("markdown_file", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--target-language", default=DEFAULT_TARGET_LANGUAGE, show_default=True,
              help="Translation prompt template, named after the target language")
@click.option("--model", "-m", help="Model name (overrides GEMINI_MODEL)")
@click.option("--custom-prompt",
              help="Translation prompt text or file; must contain {text}, "
                   "may use {context_section} and {source_language}")
@click.option("--prompts-dir", type=click.Path(file_okay=False, path_type=Path),
              help="Extra prompt directory searched before the built-in one (env: BASIRA_PROMPTS_DIR)")
@click.option("--output", "-o", type=click.Path(dir_okay=False, path_type=Path),
              help="Output markdown (default: <name>_<language>.md next to the input)")
@click.option("--resume", is_flag=True, help="Keep finished pages and translate only missing or failed ones")
@click.option("--force-restart", is_flag=True, help="Delete the existing translation and start over")
@_command("translate")
def translate(reporter, markdown_file, target_language, model, custom_prompt, prompts_dir, output,
              resume, force_restart):
    """Translate an OCR markdown file page by page with a sliding window.

    Each page is sent with the previous page's finished translation as
    context. A failed page clears the window for the next one.
    """
    _check_mode(resume, force_restart)
    settings = Settings()
    target_language = target_language.strip().lower()
    loader = PromptLoader(prompts_dir or settings.prompts_dir)

    if custom_prompt:
        template = read_custom_prompt(custom_prompt)
    else:
        template = loader.load("translate", target_language)
    check_translation_template(template)

    source_meta, pages = read_page_document(markdown_file.read_text(encoding="utf-8"))
    if not pages:
        raise CommandError(f"no pages found in {display_path(markdown_file)}")
    source_language = str(source_meta.get("source_language") or "").strip()

    output_path = output or default_output_path(markdown_file, target_language, loader.available("translate"))
    rerun = [
        "translate", markdown_file,
        "--target-language", target_language,
        *_option("--custom-prompt", custom_prompt),
        *_option("--prompts-dir", prompts_dir),
        *_option("--model", model),
        *_option("--output", output),
    ]
    _refuse_overwrite(output_path, len(pages), resume, force_restart, rerun)

    model_name = model or settings.gemini_model
    provider = _connect(settings, model, reporter)
    replacing = force_restart and output_path.exists()
    try:
        source_file = str(markdown_file.resolve().relative_to(output_path.resolve().parent))
    except ValueError:
        source_file = str(markdown_file.resolve())
    page_file = PageFile(
        output_path,
        SeparatorType.TYPST,
        {
            "title": source_meta.get("title") or "",
            "author": source_meta.get("author") or "",
            "source_file": source_file,
            "source_language": source_language or None,
            "target_language": target_language,
            "ai_provider": PROVIDER_NAME,
            "ai_model": model_name,
            "translated": _now(),
            "processing_status": "In progress",
        },
        resume=resume,
    )
    completed = page_file.completed()
    todo = [i for i, page in enumerate(pages) if page.number not in completed]

    details = [f"{len(pages)} pages"]
    if completed:
        details.append(f"{len(completed & {p.number for p in pages})} done earlier")
    details += [f"{source_language or 'auto'} → {target_language}", _model_details(settings, model, provider)]
    reporter.emit(RunStarted("translate", display_path(markdown_file), display_path(output_path),
                             " · ".join(details), pages=[p.number for p in pages],
                             todo=[pages[i].number for i in todo], done_before=sorted(completed)))
    if replacing:
        reporter.warn("starting over: the previous translation was deleted")
    if not todo:
        reporter.warn("nothing to do: every page is already translated")

    source_display = source_language.capitalize() if source_language else "Auto"

    def context_note(index: int) -> str:
        if index == 0:
            return "translating · first page, no context"
        previous = pages[index - 1]
        if previous_page_context(pages, index, page_file.pages) is not None:
            return f"translating · context: page {previous.number}"
        return f"translating · context cleared (page {previous.number} has no translation)"

    def translate_page(index: int):
        page = pages[index]

        def work() -> str:
            context = previous_page_context(pages, index, page_file.pages)
            return provider.generate_text(render_prompt(template, page.text, context, source_display))

        return work

    tasks = []
    for index in todo:
        page = pages[index]
        if page.is_error:
            tasks.append(PageTask(page.number, skip_reason=f"no OCR text in the source ({page.error})"))
        elif not page.text:
            tasks.append(PageTask(page.number, lambda: "", "empty page"))
        else:
            tasks.append(PageTask(page.number, translate_page(index), functools.partial(context_note, index)))
    result = run_pages(tasks, page_file, provider, reporter, {p.number for p in pages})

    hints = _resume_hints(result, rerun)
    if any(page.is_error for page in pages):
        hints.append(Hint("source", f"fix the failed OCR pages in {display_path(markdown_file)} first",
                          note="write the text under their markers, then rerun with --resume"))
    hints.append(Hint("output", display_path(output_path)))
    if result.outcome == "done" and not page_file.failed():
        hints.append(Hint("next", command_line("typst", display_path(output_path)), command=True,
                          note="after reviewing the translation"))
    return _finish(reporter, "translate", result, page_file, len(pages), hints)


# ---------------------------------------------------------------------------- typst


@main.command()
@click.argument("markdown_file", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--title", help="Book title (overrides the frontmatter)")
@click.option("--author", help="Book author (overrides the frontmatter)")
@click.option("--subtitle", help="Subtitle under the title")
@click.option("--version", help="Edition line above the date, e.g. 'Second edition'")
@click.option("--date", help="Date on the title page (default: current year)")
@click.option("--translator-preface", type=click.Path(exists=True, dir_okay=False, path_type=Path),
              help="Markdown preface inserted after the title page")
@click.option("--lang", help="Document language code, e.g. en, ru (default: from the frontmatter)")
@click.option("--header", help="Running header text (default: the title)")
@click.option("--toc-title", help="Table of contents heading (default depends on --lang)")
@click.option("--toc/--no-toc", default=None, help="Show or hide the table of contents")
@click.option("--template", type=click.Path(exists=True, dir_okay=False, path_type=Path),
              help="Your own copy of the template (start from `basira export template`)")
@click.option("--output", "-o", type=click.Path(dir_okay=False, path_type=Path),
              help="Output PDF (default: next to the markdown)")
@_command("typst")
def typst(reporter, markdown_file, title, author, translator_preface, toc, template, output, **variables):
    """Typeset a markdown file into a book PDF with Pandoc and Typst.

    Title and author come from the YAML frontmatter unless given here.
    """
    missing_tools = typst_processor.missing_tools()
    if missing_tools:
        raise CommandError(f"{' and '.join(missing_tools)} not found on PATH", [
            Hint("pandoc", "https://pandoc.org/installing.html"),
            Hint("typst", "https://github.com/typst/typst#installation"),
        ])

    content = markdown_file.read_text(encoding="utf-8")
    meta, pages = read_page_document(content)
    metadata = typst_processor.resolve_metadata(
        content, title, author, str(translator_preface) if translator_preface else None, variables.get("lang")
    )
    missing = [name for name in ("title", "author") if not metadata["title" if name == "title" else "authors"]]
    if missing:
        raise CommandError(f"no {' and '.join(missing)} for {display_path(markdown_file)}", [
            Hint("add", f"{' and '.join(f'{m}:' for m in missing)} to its YAML frontmatter"),
            Hint("or pass", command_line("typst", markdown_file, "--title", Placeholder('"..."'),
                                         "--author", Placeholder('"..."')), command=True),
        ])

    output_path = output or markdown_file.with_suffix(".pdf")
    template_name = display_path(template) if template else "A5 template"
    details = [metadata["title"], ", ".join(metadata["authors"]), metadata["lang"] or "en", template_name]
    reporter.emit(RunStarted("typst", display_path(markdown_file), display_path(output_path), " · ".join(details)))

    failed = sorted(p.number for p in pages if p.is_error)
    if failed:
        retry = ""
        if meta.get("source_file") and meta.get("target_language"):
            source = display_path(markdown_file.parent / str(meta["source_file"]))
            retry = "retry first: " + command_line("translate", source, "--target-language",
                                                    meta["target_language"], "--resume")
        verb = "is" if len(failed) == 1 else "are"
        reporter.warn(f"{pages_label(failed)} failed in an earlier step and {verb} left out of the book", hint=retry)
    elif str(meta.get("processing_status", "")).lower() == "incomplete":
        reporter.warn("the file is marked Incomplete, so some pages may be missing from the book")

    started = time.monotonic()

    def on_step(name: str, seconds: Optional[float]) -> None:
        reporter.emit(StepStarted(name) if seconds is None else StepDone(name, seconds))

    typst_processor.generate_pdf(
        markdown_file,
        output_path,
        title=title,
        author=author,
        translator_preface=str(translator_preface) if translator_preface else None,
        toc=toc,
        template_path=template or typst_processor.TEMPLATE_PATH,
        on_step=on_step,
        **variables,
    )
    with pymupdf.open(output_path) as pdf:
        pdf_pages = len(pdf)
    size_kb = output_path.stat().st_size / 1024
    reporter.emit(RunFinished(
        stage="typst",
        outcome="done",
        elapsed=time.monotonic() - started,
        extra=f"{pdf_pages} pages · {size_kb:,.0f} KB",
        hints=[Hint("output", display_path(output_path))],
    ))
    print_result_path(output_path)
    return 0


# ---------------------------------------------------------------------------- export


@main.group()
def export():
    """Write copies of the built-in template or prompts, to edit and use as your own."""


@export.command("template")
@click.argument("destination", required=False, default="template.typ",
                type=click.Path(dir_okay=False, path_type=Path))
@click.option("--force", is_flag=True, help="Overwrite the file if it exists")
@_command("export")
def export_template(reporter, destination, force):
    """Copy the book template to DESTINATION (default: ./template.typ).

    Use it with: basira typst book.md --template template.typ
    """
    if destination.exists() and not force:
        raise CommandError(f"{display_path(destination)} already exists. Nothing was changed.", [
            Hint("overwrite", command_line("export", "template", destination, "--force"), command=True),
        ])
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(typst_processor.TEMPLATE_PATH, destination)
    reporter.emit(RunFinished(stage="export", outcome="done", extra="template written", hints=[
        Hint("file", display_path(destination)),
        Hint("use it", command_line("typst", Placeholder("book.md"), "--template", destination), command=True),
    ]))
    print_result_path(destination)
    return 0


@export.command("prompts")
@click.argument("destination", required=False, default="prompts",
                type=click.Path(file_okay=False, path_type=Path))
@click.option("--force", is_flag=True, help="Overwrite prompt files that already exist")
@_command("export")
def export_prompts(reporter, destination, force):
    """Copy all OCR and translation prompts to DESTINATION (default: ./prompts).

    Use them with --prompts-dir DESTINATION (or BASIRA_PROMPTS_DIR).
    """
    sources = sorted(BUILTIN_PROMPTS_DIR.rglob("*.txt"))
    targets = [(source, destination / source.relative_to(BUILTIN_PROMPTS_DIR)) for source in sources]
    existing = [target for _, target in targets if target.exists()]
    if existing and not force:
        raise CommandError(
            f"{len(existing)} prompt file(s) already exist in {display_path(destination)}. Nothing was changed.",
            [Hint("overwrite", command_line("export", "prompts", destination, "--force"), command=True)],
        )
    for source, target in targets:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    reporter.emit(RunFinished(stage="export", outcome="done", extra=f"{len(targets)} prompts written", hints=[
        Hint("folder", display_path(destination)),
        Hint("use them", command_line("ocr", Placeholder("book.pdf"), "--prompts-dir", destination), command=True,
             note="same for translate; or set BASIRA_PROMPTS_DIR"),
    ]))
    print_result_path(destination)
    return 0


if __name__ == "__main__":
    main()
