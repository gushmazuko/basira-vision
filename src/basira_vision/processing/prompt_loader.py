"""Prompt template lookup.

Templates are plain text files named ``<stage>/<language>.txt``. The
built-in set ships inside the package; an optional user directory with the
same layout is searched first, so it can add languages or override built-ins.
"""

from pathlib import Path
from typing import List, Optional

from ..utils.errors import PromptLoadingError
from ..utils.logging import get_logger

logger = get_logger("prompts")

BUILTIN_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
STAGES = ("ocr", "translate")


class PromptLoader:
    """Find and load prompt templates for a stage and a language."""

    def __init__(self, user_dir: Optional[Path] = None, builtin_dir: Path = BUILTIN_PROMPTS_DIR):
        self.search_dirs = [d for d in (user_dir, builtin_dir) if d is not None]

    def available(self, stage: str) -> List[str]:
        """Languages that have a template for the stage."""
        languages = set()
        for directory in self.search_dirs:
            languages.update(p.stem for p in (directory / stage).glob("*.txt"))
        return sorted(languages)

    def load(self, stage: str, language: str) -> str:
        """Load ``<stage>/<language>.txt``; unknown languages raise with the available list."""
        language = language.strip().lower()
        for directory in self.search_dirs:
            path = directory / stage / f"{language}.txt"
            if path.is_file():
                content = path.read_text(encoding="utf-8").strip()
                if not content:
                    raise PromptLoadingError(f"Prompt template {path} is empty")
                logger.debug(f"Loaded prompt {path} ({len(content)} chars)")
                return content
        raise PromptLoadingError(
            f"No {stage} prompt for language '{language}'. "
            f"Available: {', '.join(self.available(stage))}. "
            f"Add your own as {stage}/{language}.txt in a --prompts-dir directory."
        )


def read_custom_prompt(value: str) -> str:
    """A --custom-prompt value is either a path to a text file or the prompt itself."""
    path = Path(value).expanduser()
    try:
        if path.is_file():
            return path.read_text(encoding="utf-8").strip()
    except OSError:
        pass
    return value
