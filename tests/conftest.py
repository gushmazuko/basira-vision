import re
from pathlib import Path

import pymupdf
import pytest

from basira_vision import cli
from basira_vision.utils.errors import VisionProviderError


class FakeProvider:
    """Stands in for the Gemini provider: no network, scripted failures and retries."""

    name = "fake"
    endpoint = "fake.local"

    def __init__(self):
        self.on_retry = None
        self.fail_pages = set()
        self.fail_texts = set()
        self.retry_pages = set()  # report one retry before succeeding
        self.raise_on_page = {}  # page -> exception instance
        self.on_page = {}  # page -> callable run before answering
        self.ocr_calls = []
        self.prompts = []
        self.connection_warning = None
        self.connection_error = None

    def check_connection(self):
        if self.connection_error:
            raise self.connection_error
        return self.connection_warning

    def extract_text(self, image_path: Path, prompt: str) -> str:
        number = int(re.search(r"(\d+)", image_path.stem).group(1))
        self.ocr_calls.append(number)
        if number in self.on_page:
            self.on_page[number]()
        if number in self.raise_on_page:
            raise self.raise_on_page[number]
        if number in self.retry_pages and self.on_retry:
            self.on_retry("HTTP 503 UNAVAILABLE", 1, 2, 0.0, False)
        if number in self.fail_pages:
            raise VisionProviderError(f"boom on page {number}")
        return f"text of page {number}"

    def generate_text(self, prompt: str) -> str:
        self.prompts.append(prompt)
        source = re.search(r"SRC\[(.*?)\]", prompt, re.S).group(1)
        if source in self.fail_texts:
            raise VisionProviderError("translation failed")
        return f"tr({source})"


@pytest.fixture
def provider(monkeypatch, tmp_path):
    fake = FakeProvider()
    monkeypatch.chdir(tmp_path)  # no stray .env, outputs land in tmp_path/workspace
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    for var in ("GEMINI_BASE_URL", "BASIRA_OUTPUT_DIR", "BASIRA_PROMPTS_DIR"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(cli, "create_provider", lambda **kwargs: fake)
    return fake


@pytest.fixture
def make_pdf(tmp_path):
    def _make(pages: int, name: str = "book.pdf") -> Path:
        path = tmp_path / name
        doc = pymupdf.open()
        for i in range(1, pages + 1):
            page = doc.new_page(width=200, height=300)
            page.insert_text((40, 60), f"Page {i}")
        doc.save(path)
        doc.close()
        return path

    return _make
