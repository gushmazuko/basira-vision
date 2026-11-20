"""Command-line interface: ``basira ocr | translate | typst``."""

import functools
import hashlib
import os
import shlex
import shutil
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import click
import pymupdf
from click.core import ParameterSource

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
from .providers.gemini import MEDIA_RESOLUTIONS, THINKING_LEVELS
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


def _overwrite_options(what: str):
    """``--overwrite``, and ``--force-restart`` as its hidden former name."""

    def decorate(func):
        func = click.option("--force-restart", "force_restart", is_flag=True, hidden=True)(func)
        return click.option("--overwrite", is_flag=True, help=what)(func)

    return decorate


def _check_mode(resume: bool, overwrite: bool, force_restart: bool) -> bool:
    """Validate the mode flags; return whether the run overwrites (under either name)."""
    overwrite = overwrite or force_restart
    if resume and overwrite:
        raise click.UsageError("--resume and --overwrite cannot be used together")
    return overwrite


def _without_range(args: List) -> List:
    """The command without its --page-range option."""
    return [arg for i, arg in enumerate(args) if arg != "--page-range" and (i == 0 or args[i - 1] != "--page-range")]


def _refuse_overwrite(path: Path, total: Optional[int], resume: bool, overwrite: bool, rerun: List,
                      replaced: Optional[List[int]] = None) -> None:
    """Never replace finished work silently: ask for --resume or --overwrite.

    ``replaced`` are the pages an --overwrite would replace when it is limited to some of them.
    """
    if not path.exists() or resume or overwrite:
        return
    done, failed, total = describe_existing(path, total)
    state = f"{done} of {total} pages done" + (f", {len(failed)} failed" if failed else "")
    keep = "keeps it and your edits"
    if failed:
        keep += f", retries {pages_label(failed)}"
    effect = f"replaces {pages_label(replaced)}, keeps the other pages" if replaced else "deletes it and starts over"
    raise CommandError(
        f"{display_path(path)} already exists ({state}). Nothing was changed.",
        [
            Hint("continue", command_line(*rerun, "--resume"), command=True, note=keep),
            Hint("overwrite", command_line(*rerun, "--overwrite"), command=True, note=effect),
        ],
    )


# A --resume (and an --overwrite of some pages) continues the same job: the settings that shape
# the text are recorded in the file's frontmatter when it is created and reused when the command
# line does not name them.
# Frontmatter key -> the command option it comes from.
_OCR_SETTINGS = {
    "source_language": "source_language",
    "ai_model": "model",
    "custom_prompt": "custom_prompt",
    "prompts_dir": "prompts_dir",
    "dpi": "dpi",
    "strips": "strips",
    "media_resolution": "media_resolution",
    "thinking_level": "thinking_level",
    "image_format": "image_format",
}
_TRANSLATE_SETTINGS = {
    "target_language": "target_language",
    "ai_model": "model",
    "custom_prompt": "custom_prompt",
    "prompts_dir": "prompts_dir",
}
_INLINE_PROMPT = "inline:"


def _as_text(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError
    return value.strip()


def _as_optional_text(value: Any) -> Optional[str]:
    return None if value is None else _as_text(value)


def _as_int(value: Any, low: int, high: int) -> int:
    if isinstance(value, bool):
        raise ValueError
    number = int(value.strip()) if isinstance(value, str) else value
    if not isinstance(number, int) or not low <= number <= high:
        raise ValueError
    return number


def _as_choice(value: Any, choices: Iterable[str]) -> Optional[str]:
    if value is None:
        return None
    for choice in choices:
        if _as_text(value).lower() == choice.lower():
            return choice
    raise ValueError


# A recorded setting is read back in the form the command line produces, so a hand edit or
# YAML typing (dpi: "150", image_format: png) neither causes a false conflict nor a crash.
_SETTING_FORMS = {
    "source_language": lambda v: _as_text(v).lower(),
    "target_language": lambda v: _as_text(v).lower(),
    "ai_model": _as_text,
    "custom_prompt": _as_optional_text,
    "prompts_dir": _as_optional_text,
    "dpi": lambda v: _as_int(v, 72, 600),
    "strips": lambda v: _as_int(v, 0, 8),
    "media_resolution": lambda v: _as_choice(v, MEDIA_RESOLUTIONS),
    "thinking_level": lambda v: _as_choice(v, THINKING_LEVELS),
    "image_format": lambda v: _as_choice(v, IMAGE_FORMATS),
}


def _typed(option: str) -> bool:
    """Whether the user gave this option on the command line (a default is not a choice)."""
    return click.get_current_context().get_parameter_source(option) == ParameterSource.COMMANDLINE


def _recorded_path(path: Optional[Path], output_path: Path) -> Optional[str]:
    """A path as recorded in the frontmatter, without the user's name where avoidable.

    Relative to the output file when both share a folder below the root; otherwise
    ``~/...`` under the home folder, or the absolute path. Always with forward slashes.
    """
    if path is None:
        return None
    resolved = path.expanduser().resolve()
    folder = output_path.resolve().parent
    try:
        shared = Path(os.path.commonpath([resolved, folder]))
    except ValueError:  # another drive on Windows
        shared = None
    if shared is not None and shared != Path(shared.anchor):
        return Path(os.path.relpath(resolved, folder)).as_posix()
    home = Path.home().resolve()
    if resolved.is_relative_to(home):
        return "~/" + resolved.relative_to(home).as_posix()
    return resolved.as_posix()


def _recorded_prompt(custom_prompt: Optional[str], output_path: Path) -> Optional[str]:
    """A --custom-prompt as recorded: its file path, or a fingerprint of inline text."""
    if not custom_prompt:
        return None
    path = Path(custom_prompt).expanduser()
    try:
        is_file = path.is_file()
    except OSError:  # long prompt text is not a valid file name
        is_file = False
    if is_file:
        return _recorded_path(path, output_path)
    return _INLINE_PROMPT + hashlib.sha256(custom_prompt.encode("utf-8")).hexdigest()[:16]


def _stored_path(value: Optional[str], output_path: Path) -> Optional[Path]:
    """Turn a recorded path back into a usable one."""
    if not value:
        return None
    path = Path(value).expanduser()
    # Lexical: on the first run the output folder, and so the ".." steps, do not exist yet.
    return path if path.is_absolute() else Path(os.path.normpath(output_path.parent / path))


def _resume_settings(output_path: Path, current: Dict[str, Any], options: Dict[str, str],
                     continue_command: List, typed_rerun: List, mode: str = "--resume") -> Dict[str, Any]:
    """The settings a --resume (or an --overwrite of some pages) continues with.

    Each setting recorded in the file wins over the default; one typed on the command line
    with a different value stops the run instead of mixing two configurations in one file.
    """
    meta, _ = read_page_document(output_path.read_text(encoding="utf-8"))
    resolved = dict(current)
    conflicts = []
    for key, option in options.items():
        if key not in meta:
            continue
        try:
            stored = _SETTING_FORMS[key](meta[key])
        except (TypeError, ValueError):
            raise CommandError(
                f"{display_path(output_path)} has an invalid {key} in its frontmatter: {meta[key]!r}. "
                "Nothing was changed.",
                [Hint("fix", "correct the value in the frontmatter, or start over with --overwrite (all pages)")],
            ) from None
        if _typed(option) and current[key] != stored:
            conflicts.append(f"--{option.replace('_', '-')} is {_shown(current[key])}, "
                             f"the file has {_shown(stored)}")
        else:
            resolved[key] = stored
    if conflicts:
        raise CommandError(
            f"{display_path(output_path)} was made with other settings ({'; '.join(conflicts)}). "
            "Nothing was changed.",
            [
                Hint("continue", command_line(*continue_command, mode), command=True,
                     note="with the file's settings"),
                Hint("restart", command_line(*_without_range(typed_rerun), "--overwrite"), command=True,
                     note="deletes the file and starts over with the new settings"),
            ],
        )
    return resolved


def _shown(value: Any) -> str:
    return "not set" if value is None else str(value)


def _require_resumable(output_path: Path, resume: bool, others: Iterable[Path] = ()) -> None:
    """--resume continues an existing file; it never starts a new one silently."""
    if not resume or output_path.exists():
        return
    hints = [Hint("start", "run the same command without --resume")]
    for other in others:
        hints.append(Hint("existing", display_path(other)))
    raise CommandError(f"nothing to resume: {display_path(output_path)} does not exist. Nothing was changed.",
                       hints)


def _load_prompt(stage: str, values: Dict[str, Any], custom_prompt: Optional[str],
                 output_path: Path, mode: str = "--resume") -> tuple:
    """Load the prompt the settings name; return (text, label, --custom-prompt value for hints)."""
    recorded = values["custom_prompt"]
    if recorded is None:
        language = values["source_language" if stage == "ocr" else "target_language"]
        prompts_dir = _stored_path(values["prompts_dir"], output_path)
        if prompts_dir is not None and not prompts_dir.is_dir():
            raise CommandError(f"the prompt directory of {display_path(output_path)} is missing: "
                               f"{display_path(prompts_dir)}",
                               [Hint("continue", "restore it, or start over with --overwrite (all pages)")])
        return PromptLoader(prompts_dir).load(stage, language), f"{stage}/{language}", None
    if custom_prompt:  # typed, and the same as recorded when resuming
        return read_custom_prompt(custom_prompt), "custom", custom_prompt
    if recorded.startswith(_INLINE_PROMPT):
        raise CommandError(
            f"{display_path(output_path)} was made with a --custom-prompt given as text. "
            "Nothing was changed.",
            [Hint("continue", f"pass the same --custom-prompt text again with {mode}")],
        )
    path = _stored_path(recorded, output_path)
    if not path.is_file():
        raise CommandError(f"the prompt file of {display_path(output_path)} is missing: {display_path(path)}",
                           [Hint("continue", "restore it at that path, or start over with --overwrite (all pages)")])
    return read_custom_prompt(str(path)), "custom", path


def _inline_prompt_option(values: Dict[str, Any], custom_prompt: Optional[str]) -> List:
    """Prompt text given inline cannot be recovered from the file: keep it in a continue hint."""
    recorded = values["custom_prompt"]
    if custom_prompt and recorded and recorded.startswith(_INLINE_PROMPT):
        return ["--custom-prompt", custom_prompt]
    return []


def _translation_of(path: Path) -> bool:
    """Whether a file next to the source is a basira translation (for 'nothing to resume' hints)."""
    try:
        return bool(read_page_document(path.read_text(encoding="utf-8"))[0].get("target_language"))
    except (OSError, UnicodeDecodeError):
        return False


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


def _resume_hints(result: RunResult, rerun: List, unreached: Optional[List[int]] = None) -> List[Hint]:
    """What to run next. ``unreached`` are pages of an --overwrite that still hold their old text:
    --resume would skip them as done, so they are continued with --overwrite."""
    resume = command_line(*rerun, "--resume")
    if unreached and result.outcome in ("paused", "stopped"):
        first, last = min(unreached), max(unreached)
        span = str(first) if first == last else f"{first}-{last}"
        hints = [Hint("continue", command_line(*_without_range(rerun), "--page-range", span, "--overwrite"),
                      command=True, note=f"replaces {pages_label(unreached)}, not reached yet"
                      + (" (once the cause is fixed)" if result.outcome == "stopped" else ""))]
        if result.failed:
            hints.append(Hint("retry", resume, command=True, note="retries only the failed pages"))
        return hints
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

    Exit codes: 0 done, 3 done with failed pages, 1 error or stopped, 2 invalid usage,
    130 paused with Ctrl-C.
    """


# ---------------------------------------------------------------------------- ocr


@main.command()
@click.argument("pdf_file", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--source-language", default="english", show_default=True,
              help="OCR prompt template, named after the language of the scan")
@click.option("--page-range", help="Pages to process, e.g. 5 or 1-10 (default: all)")
@click.option("--dpi", type=click.IntRange(72, 600), default=300, show_default=True,
              help="Render resolution; for small print use --strips, not a higher dpi")
@click.option("--strips", type=click.IntRange(0, 8), default=0, show_default=True,
              help="Also send N overlapping full-resolution strips of each page in the same request")
@click.option("--media-resolution", type=click.Choice(MEDIA_RESOLUTIONS, case_sensitive=False),
              help="Token budget per image (model default if not set)")
@click.option("--thinking-level", type=click.Choice(THINKING_LEVELS, case_sensitive=False),
              help="Reasoning effort (model default if not set)")
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
@_overwrite_options("Replace the existing output: all of it, or only the pages of --page-range "
                    "(the other pages and the file's settings stay)")
@_command("ocr")
def ocr(reporter, pdf_file, source_language, page_range, dpi, strips, media_resolution, thinking_level, model,
        custom_prompt, prompts_dir, output_dir, image_format, no_images, resume, overwrite, force_restart):
    """Read every page of a scanned PDF with a vision model into markdown.

    Writes <workspace>/<name>/<name>.md with one <!-- Page N --> block per page.
    """
    overwrite = _check_mode(resume, overwrite, force_restart)
    settings = Settings()

    total = page_count(pdf_file)
    try:
        requested = parse_page_range(page_range, total)
    except ValueError as e:
        raise click.BadParameter(str(e), param_hint="--page-range") from None

    document_dir = settings.workspace_dir(output_dir) / pdf_file.stem
    output_path = document_dir / f"{pdf_file.stem}.md"
    base = ["ocr", pdf_file, *_option("--page-range", page_range), *_option("--output-dir", output_dir),
            *(["--no-images"] if no_images else [])]
    run_settings = {
        "source_language": source_language.strip().lower(),
        "ai_model": model or settings.gemini_model,
        "custom_prompt": _recorded_prompt(custom_prompt, output_path),
        "prompts_dir": _recorded_path(prompts_dir or settings.prompts_dir, output_path),
        "dpi": dpi,
        "strips": strips,
        "media_resolution": media_resolution and media_resolution.lower(),
        "thinking_level": thinking_level and thinking_level.lower(),
        "image_format": image_format.upper(),
    }

    def command(values: Dict[str, Any], prompt_arg: Optional[str]) -> List:
        return [
            *base,
            *_option("--source-language", values["source_language"], "english"),
            *_option("--custom-prompt", prompt_arg),
            *_option("--prompts-dir", _stored_path(values["prompts_dir"], output_path)),
            *_option("--model", model),
            *_option("--dpi", values["dpi"], 300),
            *_option("--strips", values["strips"], 0),
            *_option("--media-resolution", values["media_resolution"]),
            *_option("--thinking-level", values["thinking_level"]),
            *_option("--image-format", values["image_format"], "PNG"),
        ]

    # An --overwrite of some pages replaces just them, so it continues the same file like --resume.
    partial = len(requested) < total
    keep_file = resume or (overwrite and partial and output_path.exists())
    _require_resumable(output_path, resume)
    if keep_file:
        run_settings = _resume_settings(output_path, run_settings, _OCR_SETTINGS,
                                        [*base, *_inline_prompt_option(run_settings, custom_prompt)],
                                        command(run_settings, custom_prompt), "--resume" if resume else "--overwrite")
    prompt, prompt_label, prompt_arg = _load_prompt("ocr", run_settings, custom_prompt, output_path,
                                                     "--resume" if resume else "--overwrite")
    source_language = run_settings["source_language"]
    model_name = run_settings["ai_model"]
    dpi, strips = run_settings["dpi"], run_settings["strips"]
    media_resolution, thinking_level = run_settings["media_resolution"], run_settings["thinking_level"]
    image_format = run_settings["image_format"]
    rerun = command(run_settings, prompt_arg)
    _refuse_overwrite(output_path, total, resume, overwrite, rerun, requested if partial else None)

    provider = _connect(settings, model_name, reporter)
    replacing = overwrite and output_path.exists()
    page_file = PageFile(
        output_path,
        SeparatorType.OCR,
        {
            "title": "",
            "author": "",
            "source_language": source_language,
            "ai_provider": PROVIDER_NAME,
            "ai_model": model_name,
            "basira_version": __version__,
            **{key: run_settings[key] for key in _OCR_SETTINGS if key not in ("source_language", "ai_model")},
            "generated": _now(),
            "processing_status": "In progress",
        },
        resume=keep_file,
    )
    completed = page_file.completed()
    todo = list(requested) if overwrite else [n for n in requested if n not in completed]

    details = [f"{total} pages"]
    if page_range:
        details.append(f"range {page_range}")
    done_before = completed - set(todo)
    if done_before:
        details.append(f"{len(done_before)} done earlier")
    details += [f"prompt {prompt_label}", _model_details(settings, model_name, provider), f"{dpi} dpi"]
    if strips:
        details.append(f"page + {strips} strips")
    if media_resolution:
        details.append(f"media resolution {media_resolution}")
    if thinking_level:
        details.append(f"thinking {thinking_level}")
    reporter.emit(RunStarted("ocr", display_path(pdf_file), display_path(output_path), " · ".join(details),
                             pages=list(range(1, total + 1)), todo=todo, done_before=sorted(done_before)))
    if replacing and keep_file:
        reporter.warn(f"replacing {pages_label(requested)}, the other pages stay")
    elif replacing:
        reporter.warn("starting over: the previous output was deleted")
    if not todo:
        reporter.warn("nothing to do: every requested page is already done")

    images_dir = None if no_images else document_dir / "images"
    with tempfile.TemporaryDirectory(prefix="basira_") as tmp, PDFRenderer(pdf_file, dpi, image_format) as renderer:
        target_dir = images_dir or Path(tmp)

        def read_page(number: int):
            return lambda: provider.extract_text(renderer.render(number, target_dir), prompt, strips,
                                                 media_resolution, thinking_level)

        tasks = [PageTask(n, read_page(n), "reading") for n in todo]
        result = run_pages(tasks, page_file, provider, reporter, set(range(1, total + 1)))

    unreached = [n for n in requested if overwrite and keep_file and n not in result.done
                 and n not in result.failed]
    hints = _resume_hints(result, rerun, unreached)
    outside = set(range(1, total + 1)) - page_file.completed() - set(page_file.failed()) - set(requested)
    if outside and result.outcome == "done":
        first, last = min(outside), max(outside)
        if last - first + 1 == len(outside):
            without_range = _without_range(rerun)
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
@_overwrite_options("Delete the existing translation and start over")
@_command("translate")
def translate(reporter, markdown_file, target_language, model, custom_prompt, prompts_dir, output,
              resume, overwrite, force_restart):
    """Translate an OCR markdown file page by page with a sliding window.

    Each page is sent with the previous page's finished translation as
    context. A failed page clears the window for the next one.
    """
    overwrite = _check_mode(resume, overwrite, force_restart)
    settings = Settings()
    target_language = target_language.strip().lower()
    loader = PromptLoader(prompts_dir or settings.prompts_dir)

    source_meta, pages = read_page_document(markdown_file.read_text(encoding="utf-8"))
    if not pages:
        raise CommandError(f"no pages found in {display_path(markdown_file)}")
    source_language = str(source_meta.get("source_language") or "").strip()

    output_path = output or default_output_path(markdown_file, target_language, loader.available("translate"))
    if output_path.resolve() == markdown_file.resolve():
        raise CommandError(f"the translation would overwrite its source {display_path(markdown_file)}",
                           [Hint("choose", "another --target-language, or an output file with -o")])
    # Without -o the output file is named after the target language, so it is part of where to look.
    base = ["translate", markdown_file, *([] if output else ["--target-language", target_language]),
            *_option("--output", output)]
    run_settings = {
        "target_language": target_language,
        "ai_model": model or settings.gemini_model,
        "custom_prompt": _recorded_prompt(custom_prompt, output_path),
        "prompts_dir": _recorded_path(prompts_dir or settings.prompts_dir, output_path),
    }

    def command(values: Dict[str, Any], prompt_arg: Optional[str]) -> List:
        return [
            "translate", markdown_file,
            "--target-language", values["target_language"],
            *_option("--output", output),
            *_option("--custom-prompt", prompt_arg),
            *_option("--prompts-dir", _stored_path(values["prompts_dir"], output_path)),
            *_option("--model", model),
        ]

    # The output file is named after the target language, so a --resume without it would look
    # for another file: name the translations that exist instead of starting a new one.
    others: List[Path] = []
    if resume and not output_path.exists() and not output:
        stem = output_path.stem.removesuffix(f"_{target_language}")
        others = [p for p in sorted(markdown_file.parent.glob(f"{stem}_*.md"))
                  if p.is_file() and p != markdown_file and _translation_of(p)]
    _require_resumable(output_path, resume, others)
    if resume:
        run_settings = _resume_settings(output_path, run_settings, _TRANSLATE_SETTINGS,
                                        [*base, *_inline_prompt_option(run_settings, custom_prompt)],
                                        command(run_settings, custom_prompt))
    template, _, prompt_arg = _load_prompt("translate", run_settings, custom_prompt, output_path)
    check_translation_template(template)
    target_language = run_settings["target_language"]
    model_name = run_settings["ai_model"]
    rerun = command(run_settings, prompt_arg)
    _refuse_overwrite(output_path, len(pages), resume, overwrite, rerun)

    provider = _connect(settings, model_name, reporter)
    replacing = overwrite and output_path.exists()
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
            "target_language": run_settings["target_language"],
            "ai_provider": PROVIDER_NAME,
            "ai_model": model_name,
            "basira_version": __version__,
            "custom_prompt": run_settings["custom_prompt"],
            "prompts_dir": run_settings["prompts_dir"],
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
    details += [f"{source_language or 'auto'} → {target_language}", _model_details(settings, model_name, provider)]
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
