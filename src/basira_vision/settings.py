"""Configuration from environment variables and an optional .env file."""

from pathlib import Path
from typing import Optional

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_MODEL = "gemini-3.8-flash"


class Settings(BaseSettings):
    """Settings shared by all commands; CLI options override them per run."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    gemini_api_key: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    )
    gemini_model: str = Field(default=DEFAULT_MODEL, validation_alias="GEMINI_MODEL")
    gemini_base_url: Optional[str] = Field(
        default=None,
        validation_alias="GEMINI_BASE_URL",
        description="Gemini-protocol service root (gateway), no /v1, /v1beta or /openai suffix",
    )
    output_dir: Optional[Path] = Field(default=None, validation_alias="BASIRA_OUTPUT_DIR")
    prompts_dir: Optional[Path] = Field(default=None, validation_alias="BASIRA_PROMPTS_DIR")

    def workspace_dir(self, override: Optional[Path] = None) -> Path:
        """Workspace root: --output-dir, then BASIRA_OUTPUT_DIR, then ./workspace."""
        directory = override or self.output_dir or Path.cwd() / "workspace"
        return directory.expanduser()
