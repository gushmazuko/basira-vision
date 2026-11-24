"""Command-line interface: ``basira ocr | capture | translate | typst``."""

import functools
import hashlib
import os
import shlex
import shutil
import signal
import socket
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Tuple

import click
import pymupdf
from click.core import ParameterSource

from . import __version__
from .capture import CaptureServer, CaptureSession, local_address, new_key
from .pipeline import GracefulInterrupt, PageTask, RunResult, run_pages
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
from .ui import LOG_FORMATS, Hint, Journal, Reporter, display_path, print_result_path
from .ui.events import CaptureStarted, RunFinished, RunStarted, StepDone, StepStarted, StopRequested
from .ui.state import pages_label
from .utils.constants import DEFAULT_TARGET_LANGUAGE, SeparatorType
from .utils.errors import AuthenticationError, BasiraError, ConfigurationError, EndpointUnreachableError
from .utils.pages import PageFile, describe_existing, read_page_document, split_frontmatter

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


def _typed_command() -> str:
    """The command as it was typed, for the first line of the log: ``basira ocr book.pdf --strips 3``.

    Built from what click parsed, not from sys.argv: the program may have been started as
    ``python -m basira_vision``. Prompt text given on the command line is left out.
    """
    ctx = click.get_current_context()
    names, level = [], ctx
    while level.parent is not None:
        names.append(level.info_name)
        level = level.parent
    args: List[Any] = names[::-1]
    for param in ctx.command.params:
        if ctx.get_parameter_source(param.name) != ParameterSource.COMMANDLINE:
            continue
        value = ctx.params.get(param.name)
        if isinstance(param, click.Argument):
            args.append(value)
            continue
        flag = max(param.opts, key=len)
        if param.is_flag:
            args += [flag] if value else [max(param.secondary_opts, key=len)] if param.secondary_opts else []
        elif param.name == "custom_prompt" and value:
            args += [flag, _prompt_file(value) or Placeholder("<text>")]
        else:
            for item in value if isinstance(value, (tuple, list)) else [value]:
                args += [flag, item]
    return command_line(*args)


def _command(stage: str, log: bool = True):
    """Shared options, the reporter's lifetime, error display and the exit code.

    ``log``: the command writes something a log can be kept next to (``Reporter.log_to``).
    """

    def decorate(func):
        @functools.wraps(func)
        def wrapper(log_format, verbose, no_log=True, **kwargs):
            journal = None if no_log else Journal(stage, __version__, _typed_command())
            with Reporter(stage, log_format, verbose, journal) as reporter:
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
                except (click.ClickException, click.exceptions.Exit, click.Abort):
                    raise  # click's own way of ending a command
                except Exception as e:
                    reporter.crashed(e)
                    raise
            sys.exit(code or 0)

        wrapper = click.option("--verbose", "-v", is_flag=True, help="Show debug details")(wrapper)
        if log:
            wrapper = click.option("--no-log", is_flag=True,
                                   help="Do not keep a log file (<output>.log) next to the output")(wrapper)
        return click.option("--log-format", type=click.Choice(LOG_FORMATS), default="auto", show_default=True,
                            help="What the terminal shows. auto: live view in a terminal, plain lines "
                                 "otherwise; json: one event per line")(wrapper)

    return decorate


def _overwrite_options(what: str):
    """``--overwrite``, and ``--force-restart`` as its hidden former name."""

    def decorate(func):
        func = click.option("--force-restart", "force_restart", is_flag=True, hidden=True)(func)
        return click.option("--overwrite", is_flag=True, help=what)(func)

    return decorate


@contextmanager
def _shielded() -> Iterator[None]:
    """Ctrl-C does nothing in here, except where a GracefulInterrupt inside takes it over."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    previous = signal.signal(signal.SIGINT, lambda signum, frame: None)
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, previous)


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
# A photo has no render resolution and is always saved as JPEG.
_CAPTURE_SETTINGS = {key: option for key, option in _OCR_SETTINGS.items() if key not in ("dpi", "image_format")}
_TRANSLATE_SETTINGS = {
    "target_language": "target_language",
    "ai_model": "model",
    "custom_prompt": "custom_prompt",
    "prompts_dir": "prompts_dir",
}
_INLINE_PROMPT = "inline:"
_START_OVER = "start over with --overwrite (all pages)"


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
    path = _prompt_file(custom_prompt)
    if path is not None:
        return _recorded_path(path, output_path)
    return _INLINE_PROMPT + hashlib.sha256(custom_prompt.encode("utf-8")).hexdigest()[:16]


def _prompt_file(custom_prompt: str) -> Optional[Path]:
    """The file a --custom-prompt names, or None when it is the prompt text itself."""
    path = Path(custom_prompt).expanduser()
    try:
        return path if path.is_file() else None
    except OSError:  # long prompt text is not a valid file name
        return None


def _stored_path(value: Optional[str], output_path: Path) -> Optional[Path]:
    """Turn a recorded path back into a usable one."""
    if not value:
        return None
    path = Path(value).expanduser()
    # Lexical: on the first run the output folder, and so the ".." steps, do not exist yet.
    return path if path.is_absolute() else Path(os.path.normpath(output_path.parent / path))


def _resume_settings(output_path: Path, current: Dict[str, Any], options: Dict[str, str],
                     continue_command: List, restart_command: Callable[[Dict[str, Any], Iterable[str]], List],
                     typed_wins: bool = False, start_over: Optional[str] = None,
                     ) -> Tuple[Dict[str, Any], Dict[str, str]]:
    """The settings a --resume (or an --overwrite of some pages) continues with.

    Each setting recorded in the file wins over the default. One typed on the command line
    with a different value stops a --resume instead of mixing two configurations in one file.
    An --overwrite of some pages is a deliberate choice for those pages (``typed_wins``): the
    typed value is used, and the differences (setting -> description) are returned so the run
    can say so.

    ``restart_command(settings, typed)`` builds the command that starts over: the file's
    settings with the typed ones on top, so starting over changes only what the user asked for.

    ``start_over``: for a command that continues an existing file by itself and has no
    --resume and --overwrite (capture), the words that say how to start over instead.
    """
    meta, raw, _ = split_frontmatter(output_path.read_text(encoding="utf-8"))
    if raw.strip() and not meta:
        # Going on would read the pages with the built-in defaults instead of the file's settings.
        raise CommandError(
            f"{display_path(output_path)} has a frontmatter that cannot be read. Nothing was changed.",
            [Hint("fix", f"correct the frontmatter, or {start_over or _START_OVER}")],
        )
    resolved = dict(current)
    conflicts: Dict[str, str] = {}
    for key, option in options.items():
        if key not in meta:
            continue
        try:
            stored = _SETTING_FORMS[key](meta[key])
        except (TypeError, ValueError):
            raise CommandError(
                f"{display_path(output_path)} has an invalid {key} in its frontmatter: {meta[key]!r}. "
                "Nothing was changed.",
                [Hint("fix", f"correct the value in the frontmatter, or {start_over or _START_OVER}")],
            ) from None
        if _typed(option) and current[key] != stored:
            conflicts[key] = _difference(option, key, current[key], stored)
        else:
            resolved[key] = stored
    if conflicts and not typed_wins:
        if start_over:
            hints = [Hint("continue", command_line(*continue_command), command=True, note="with the file's settings"),
                     Hint("restart", start_over, note="with the new settings")]
        else:
            hints = [
                Hint("continue", command_line(*continue_command, "--resume"), command=True,
                     note="with the file's settings"),
                Hint("restart", command_line(*_without_range(restart_command(resolved, conflicts)), "--overwrite"),
                     command=True, note="deletes the file and starts over with the new settings"),
            ]
        raise CommandError(
            f"{display_path(output_path)} was made with other settings ({'; '.join(conflicts.values())}). "
            "Nothing was changed.", hints)
    return resolved, conflicts


def _shown(value: Any) -> str:
    if value is None:
        return "not set"
    return "inline text" if str(value).startswith(_INLINE_PROMPT) else str(value)


def _difference(option: str, key: str, typed: Any, stored: Any) -> str:
    """``--strips is 2, the file has 3``: how a typed setting differs from the file's."""
    name = f"--{option.replace('_', '-')}"
    if key == "custom_prompt" and typed and stored and typed.startswith(_INLINE_PROMPT) \
            and stored.startswith(_INLINE_PROMPT):
        return f"{name} is another text than the file's"
    return f"{name} is {_shown(typed)}, the file has {_shown(stored)}"


def _hint_prompt(values: Dict[str, Any], custom_prompt: Optional[str], output_path: Path) -> Any:
    """The --custom-prompt value that reproduces these settings in a suggested command."""
    recorded = values["custom_prompt"]
    if recorded is None:
        return None
    if custom_prompt:  # typed: the settings carry it
        return custom_prompt
    if recorded.startswith(_INLINE_PROMPT):  # the text itself is not in the file
        return Placeholder("<the same text>")
    return _stored_path(recorded, output_path)


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
                 output_path: Path, mode: str = "--resume", from_file: bool = True,
                 start_over: Optional[str] = None) -> tuple:
    """Load the prompt the settings name; return (text, label, --custom-prompt value for hints).

    ``from_file``: the prompt directory is the one recorded in the output file (a continued
    run), not one given for this run.
    """
    recorded = values["custom_prompt"]
    if recorded is None:
        language = values["source_language" if stage == "ocr" else "target_language"]
        prompts_dir = _stored_path(values["prompts_dir"], output_path)
        if prompts_dir is not None and not prompts_dir.is_dir():
            if not from_file:
                raise CommandError(f"the prompt directory does not exist: {display_path(prompts_dir)}",
                                   [Hint("check", "--prompts-dir, or BASIRA_PROMPTS_DIR in the environment")])
            raise CommandError(f"the prompt directory of {display_path(output_path)} is missing: "
                               f"{display_path(prompts_dir)}",
                               [Hint("continue", f"restore it, or {start_over or _START_OVER}")])
        return PromptLoader(prompts_dir).load(stage, language), f"{stage}/{language}", None
    if custom_prompt:  # typed, and the same as recorded when resuming
        return read_custom_prompt(custom_prompt), "custom", custom_prompt
    if recorded.startswith(_INLINE_PROMPT):
        raise CommandError(
            f"{display_path(output_path)} was made with a --custom-prompt given as text. "
            "Nothing was changed.",
            [Hint("continue", "pass the same --custom-prompt text again" + (f" with {mode}" if mode else ""))],
        )
    path = _stored_path(recorded, output_path)
    if not path.is_file():
        raise CommandError(f"the prompt file of {display_path(output_path)} is missing: {display_path(path)}",
                           [Hint("continue", f"restore it at that path, or {start_over or _START_OVER}")])
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


def _span(pages: Iterable[int]) -> Optional[str]:
    """``5`` or ``5-9`` for pages without a gap, as --page-range takes them; None otherwise."""
    pages = sorted(pages)
    if not pages or pages[-1] - pages[0] + 1 != len(pages):
        return None
    return str(pages[0]) if len(pages) == 1 else f"{pages[0]}-{pages[-1]}"


def _resume_hints(result: RunResult, rerun: List, unreached: Optional[List[int]] = None,
                  typed_settings: bool = False) -> List[Hint]:
    """What to run next.

    ``unreached`` are pages of an --overwrite that still hold their old text: --resume would
    skip them as done, so they are continued with --overwrite. ``typed_settings``: the pages
    were read with typed settings that differ from the file, which --resume refuses, so the
    failed ones are read again with --overwrite too.
    """
    resume = command_line(*rerun, "--resume")
    retry = Hint("retry", resume, command=True, note="retries only the failed pages")
    if typed_settings and result.failed:
        span = _span(result.failed)
        if span:
            retry = Hint("retry", command_line(*_without_range(rerun), "--page-range", span, "--overwrite"),
                         command=True, note=f"reads {pages_label(result.failed)} again with the same settings"
                         + (", once the cause is fixed" if result.outcome == "stopped" else ""))
        else:
            retry = Hint("retry", f"{pages_label(result.failed)} failed",
                         note="read them again with --page-range … --overwrite and the same options")
    if unreached and result.outcome in ("paused", "stopped"):
        hints = [Hint("continue",
                      command_line(*_without_range(rerun), "--page-range", _span(unreached), "--overwrite"),
                      command=True, note=f"replaces {pages_label(unreached)}, not reached yet"
                      + (" (once the cause is fixed)" if result.outcome == "stopped" else ""))]
        if result.failed:
            hints.append(retry)
        return hints
    if typed_settings:
        return [retry] if result.failed else []
    if result.outcome == "paused":
        return [Hint("continue", resume, command=True)]
    if result.outcome == "stopped":
        return [Hint("continue", resume, command=True, note="once the cause is fixed")]
    if result.failed:
        return [retry]
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

    A paper book and no scan? basira capture NAME reads its pages from a
    phone camera into the same markdown as step 1.

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
                    "(the other pages stay; options you type apply to the replaced pages only)")
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
    reporter.log_to(output_path)
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

    def command(values: Dict[str, Any], prompt_arg: Optional[str], keep: Iterable[str] = ()) -> List:
        """The command line for these settings. A default is left out, unless the setting is in
        ``keep``: typed against the file, it has to be typed again to win over the file."""

        def default(key: str, value: Any) -> Any:
            return None if key in keep else value

        return [
            *base,
            *_option("--source-language", values["source_language"], default("source_language", "english")),
            # typed empty against a file with a prompt: "no custom prompt" has to be typed again too
            *(["--custom-prompt", ""] if prompt_arg is None and "custom_prompt" in keep
              else _option("--custom-prompt", prompt_arg)),
            *_option("--prompts-dir", _stored_path(values["prompts_dir"], output_path)),
            *_option("--model", values["ai_model"], default("ai_model", settings.gemini_model)),
            *_option("--dpi", values["dpi"], default("dpi", 300)),
            *_option("--strips", values["strips"], default("strips", 0)),
            *_option("--media-resolution", values["media_resolution"]),
            *_option("--thinking-level", values["thinking_level"]),
            *_option("--image-format", values["image_format"], default("image_format", "PNG")),
        ]

    # An --overwrite of some pages replaces just them, so it continues the same file like --resume:
    # what the user did not type comes from the file. What they typed is their choice for those pages.
    partial = len(requested) < total
    keep_file = resume or (overwrite and partial and output_path.exists())
    typed_settings: Dict[str, str] = {}
    _require_resumable(output_path, resume)
    if keep_file:
        run_settings, typed_settings = _resume_settings(
            output_path, run_settings, _OCR_SETTINGS,
            [*base, *_inline_prompt_option(run_settings, custom_prompt)],
            lambda values, typed: command(values, _hint_prompt(values, custom_prompt, output_path), typed),
            typed_wins=not resume)
    prompt, prompt_label, prompt_arg = _load_prompt("ocr", run_settings, custom_prompt, output_path,
                                                     "--resume" if resume else "--overwrite",
                                                     from_file=keep_file and not _typed("prompts_dir"))
    source_language = run_settings["source_language"]
    model_name = run_settings["ai_model"]
    dpi, strips = run_settings["dpi"], run_settings["strips"]
    media_resolution, thinking_level = run_settings["media_resolution"], run_settings["thinking_level"]
    image_format = run_settings["image_format"]
    rerun = command(run_settings, prompt_arg, typed_settings)
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
    reporter.start_log()
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
        verb, pronoun = ("is", "it is") if len(requested) == 1 else ("are", "they are")
        # With a custom prompt the language and the prompt directory do not choose the prompt.
        unused = {"source_language", "prompts_dir"} if run_settings["custom_prompt"] is not None else set()
        differing = [text for key, text in typed_settings.items() if key not in unused]
        if differing:
            reporter.warn(f"{pages_label(requested)} {verb} read with other settings than the file "
                          f"({'; '.join(differing)}); the frontmatter keeps the file's settings")
        ignored = [f"--{_OCR_SETTINGS[key].replace('_', '-')}" for key in typed_settings if key in unused]
        if ignored:
            reporter.warn(f"{' and '.join(ignored)} {'has' if len(ignored) == 1 else 'have'} no effect on "
                          f"{pages_label(requested)}: {pronoun} read with the --custom-prompt")
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
    hints = _resume_hints(result, rerun, unreached, bool(typed_settings))
    outside = set(range(1, total + 1)) - page_file.completed() - set(page_file.failed()) - set(requested)
    if outside and result.outcome == "done":
        span = _span(outside)
        if span and not typed_settings:  # a --resume refuses typed settings that differ from the file
            hints.append(Hint("remaining", command_line(*_without_range(rerun), "--page-range", span, "--resume"),
                              command=True, note=f"{pages_label(outside)} not processed yet"))
        else:
            how = f"process {'it' if len(outside) == 1 else 'them'} with --page-range … --resume"
            if typed_settings:
                how += ", without the options typed for this run"
            hints.append(Hint("remaining", f"{pages_label(outside)} not processed yet", note=how))
    earlier = sorted(set(page_file.failed()) - set(result.failed))
    if earlier and result.outcome == "done":
        # This run did not touch them; without a word they would look finished.
        hints.append(Hint("failed", f"{pages_label(earlier)} failed in an earlier run",
                          note=f"retry {'it' if len(earlier) == 1 else 'them'} with --resume"))
    hints.append(Hint("output", display_path(output_path)))
    if images_dir:
        hints.append(Hint("images", display_path(images_dir)))
    if result.outcome == "done" and not page_file.failed() and not outside:
        next_step = command_line("translate", output_path, "--target-language", Placeholder("<language>"))
        hints.append(Hint("next", next_step, command=True, note="after reviewing the markdown"))
    return _finish(reporter, "ocr", result, page_file, total, hints)


# ---------------------------------------------------------------------------- capture


def _book_name(ctx, param, value: str) -> str:
    name = value.strip()
    # A name that starts with "-" could not be typed again: the next command would read it as an option.
    if not name or name[0] in ".-" or "/" in name or "\\" in name or not name.isprintable():
        raise click.BadParameter("give the book a plain name without folders; "
                                 "it becomes <workspace>/<name>/<name>.md")
    return name


@main.command()
@click.argument("name", callback=_book_name)
@click.option("--source-language", default="english", show_default=True,
              help="OCR prompt template, named after the language of the book")
@click.option("--strips", type=click.IntRange(0, 8), default=0, show_default=True,
              help="Also send N overlapping full-resolution strips of each photo in the same request")
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
@click.option("--host", default="0.0.0.0", show_default=True,
              help="Address to listen on; the default accepts phones on the local network")
@click.option("--port", type=click.IntRange(0, 65535), default=8765, show_default=True,
              help="Port to listen on (0: any free port)")
@_command("capture")
def capture(reporter, name, source_language, strips, media_resolution, thinking_level, model, custom_prompt,
            prompts_dir, output_dir, host, port):
    """Read the pages of a paper book from a phone camera.

    Shows a QR code and an address to open on a phone that is on the same
    network. Every photo taken there is one page: it is uploaded, read like ocr reads a
    scanned page, and written to <workspace>/<name>/<name>.md.

    Run the same command again to continue a book. Ctrl-C ends the session.
    """
    settings = Settings()
    document_dir = settings.workspace_dir(output_dir) / name
    output_path = document_dir / f"{name}.md"
    images_dir = document_dir / "images"
    reporter.log_to(output_path)
    base = ["capture", name, *_option("--output-dir", output_dir), *_option("--host", host, "0.0.0.0"),
            *_option("--port", port, 8765)]
    run_settings = {
        "source_language": source_language.strip().lower(),
        "ai_model": model or settings.gemini_model,
        "custom_prompt": _recorded_prompt(custom_prompt, output_path),
        "prompts_dir": _recorded_path(prompts_dir or settings.prompts_dir, output_path),
        "strips": strips,
        "media_resolution": media_resolution and media_resolution.lower(),
        "thinking_level": thinking_level and thinking_level.lower(),
    }

    # An existing book is continued, with its own settings: there is no --resume to type.
    continued = output_path.exists()
    start_over = (f"delete {display_path(output_path)} and run the command again "
                  "(the photos are kept and read again)")
    rerun = [*base, *_inline_prompt_option(run_settings, custom_prompt)]
    keep_incomplete = False
    if continued:
        meta, raw, body = split_frontmatter(output_path.read_text(encoding="utf-8"))
        # A header that cannot be read at all gets its own message from _resume_settings.
        if (meta or not raw.strip()) and ("target_language" in meta or "source_language" not in meta):
            raise CommandError(
                f"{display_path(output_path)} exists and is not a book read by basira ocr or capture. "
                "Nothing was changed.",
                [Hint("try", "another name for the book, or move the file away")])
        # A book that ocr left incomplete may lack pages of its PDF, which only ocr can tell:
        # a capture session does not turn it into a complete one.
        keep_incomplete = "dpi" in meta and meta.get("processing_status") == "Incomplete"
        if meta and not body.strip() and not any(images_dir.glob("page_*")):
            # A session that ended before its first photo left only the header: nothing is
            # continued, so the options typed now are the book's.
            continued = False
    if continued:
        # A typed inline prompt that is not the book's must not come back in the way on.
        same_prompt = meta.get("custom_prompt") == run_settings["custom_prompt"]
        run_settings, _ = _resume_settings(output_path, run_settings, _CAPTURE_SETTINGS,
                                           rerun if same_prompt else base, None, start_over=start_over)
        rerun = [*base, *_inline_prompt_option(run_settings, custom_prompt)]
    prompt, prompt_label, _ = _load_prompt("ocr", run_settings, custom_prompt, output_path, "",
                                           from_file=continued and not _typed("prompts_dir"),
                                           start_over=start_over)
    model_name, strips = run_settings["ai_model"], run_settings["strips"]
    media_resolution, thinking_level = run_settings["media_resolution"], run_settings["thinking_level"]

    provider = _connect(settings, model_name, reporter)
    try:
        server = CaptureServer(None, host, port, new_key())
    except OSError as e:
        raise CommandError(f"cannot listen on {host}:{port}: {e.strerror or e}",
                           [Hint("try", "another port with --port, or --port 0 for any free one")]) from None

    def run_session() -> int:
        started = time.monotonic()
        session: Optional[CaptureSession] = None
        try:
            # From here on a Ctrl-C ends the session in order, also one that comes before the first photo.
            # The signal handler only raises a flag: it runs on this thread, in the middle of whatever
            # this thread was doing, so it must not take a lock (the session's, the console's).
            with GracefulInterrupt(lambda: None) as interrupt:
                page_file = PageFile(
                    output_path,
                    SeparatorType.OCR,
                    {
                        "title": "",
                        "author": "",
                        "source_language": run_settings["source_language"],
                        "ai_provider": PROVIDER_NAME,
                        "ai_model": model_name,
                        "basira_version": __version__,
                        **{key: run_settings[key] for key in _CAPTURE_SETTINGS
                           if key not in ("source_language", "ai_model")},
                        "generated": _now(),
                        "processing_status": "In progress",
                    },
                    resume=continued,
                )
                reporter.start_log()
                if continued:
                    page_file.set_field("processing_status", "In progress")
                session = CaptureSession(
                    page_file, images_dir,
                    lambda image: provider.extract_text(image, prompt, strips, media_resolution, thinking_level),
                    reporter.emit,
                )
                provider.on_retry = session.on_retry

                done_before, to_read = sorted(page_file.completed()), session.unread()
                details = []
                if done_before:
                    details.append(f"{len(done_before)} done earlier")
                if to_read:
                    details.append(f"{len(to_read)} to read")
                details += [f"prompt {prompt_label}", _model_details(settings, model_name, provider)]
                if strips:
                    details.append(f"page + {strips} strips")
                if media_resolution:
                    details.append(f"media resolution {media_resolution}")
                if thinking_level:
                    details.append(f"thinking {thinking_level}")
                address = host if host not in ("0.0.0.0", "") else (local_address() or socket.gethostname())
                no_photo = {n: reason for n, reason in page_file.failed().items() if n not in to_read}
                reporter.emit(CaptureStarted("capture", "phone camera", display_path(output_path), " · ".join(details),
                                             todo=to_read, done_before=done_before,
                                             url=f"http://{address}:{server.port}/{server.key}/",
                                             failed_before=no_photo))
                server.session = session
                if not interrupt.requested:
                    server.start()
                    session.start()
                while not interrupt.requested:
                    time.sleep(0.1)
                reading = session.reading
                if reading is not None:
                    reporter.emit(StopRequested(reading))
                session.close()  # no more photos are taken; the page being read finishes
        except KeyboardInterrupt:  # a second Ctrl-C: do not wait for the page being read
            if session is not None:
                session.abort()
        finally:
            provider.on_retry = None
            server.close()
        if session is None:  # interrupted before there was a session
            raise KeyboardInterrupt

        session.finish(keep_incomplete)
        unread, failed, completed = session.unread(), page_file.failed(), page_file.completed()
        result = RunResult("done", done=completed, failed=failed, elapsed=time.monotonic() - started,
                           retries=session.retries)
        hints = []
        not_read = f"reads {pages_label(unread)}, not read yet" if unread else ""
        if session.stopped:
            result.outcome, result.reason = "stopped", session.stopped
            hints.append(Hint("continue", command_line(*rerun), command=True,
                              note="; ".join(x for x in ("once the cause is fixed", not_read) if x)))
        elif unread:
            result.outcome = "paused"
            hints.append(Hint("continue", command_line(*rerun), command=True, note=not_read))
        elif failed:
            # Running again reads a failed page from its photo; one without a photo can only be retaken.
            with_photo = [n for n in sorted(failed) if n in session.photos()]
            without = [n for n in sorted(failed) if n not in with_photo]
            if with_photo:
                it = "it" if len(with_photo) == 1 else "them"
                hints.append(Hint("retry", command_line(*rerun), command=True,
                                  note=f"reads {pages_label(with_photo)} again, or retake {it} from the phone"))
            if without:
                it, has = ("it", "has") if len(without) == 1 else ("them", "have")
                hints.append(Hint("retake", f"{pages_label(without)} {has} no photo",
                                  note=f"retake {it} from the phone"))
        hints.append(Hint("output", display_path(output_path)))
        if images_dir.is_dir():
            hints.append(Hint("images", display_path(images_dir)))
        if result.outcome == "done" and completed and not failed and not keep_incomplete:
            next_step = command_line("translate", output_path, "--target-language", Placeholder("<language>"))
            hints.append(Hint("next", next_step, command=True, note="after reviewing the markdown"))
        # A failed page whose photo waits to be read again is one page, not two.
        return _finish(reporter, "capture", result, page_file, len(set(page_file.pages) | set(unread)), hints)

    # A Ctrl-C that comes while the session is already ending (the server closing, the summary)
    # must not cut the ending short: it would leave no summary and a status of "In progress".
    with _shielded():
        return run_session()


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
    reporter.log_to(output_path)
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
            *_option("--model", values["ai_model"], settings.gemini_model),
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
        run_settings, _ = _resume_settings(output_path, run_settings, _TRANSLATE_SETTINGS,
                                           [*base, *_inline_prompt_option(run_settings, custom_prompt)],
                                           lambda values, typed: command(
                                               values, _hint_prompt(values, custom_prompt, output_path)))
    template, _, prompt_arg = _load_prompt("translate", run_settings, custom_prompt, output_path,
                                           from_file=resume and not _typed("prompts_dir"))
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
    reporter.start_log()
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
    output_path = output or markdown_file.with_suffix(".pdf")
    reporter.log_to(output_path)
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
    output_path.parent.mkdir(parents=True, exist_ok=True)
    reporter.start_log()

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
@_command("export", log=False)
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
@_command("export", log=False)
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
