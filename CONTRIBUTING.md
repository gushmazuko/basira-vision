# Contributing

Thanks for helping. New languages (prompt templates) are the most useful
contribution, followed by bug reports with a small reproducible example.

## Development setup

```bash
git clone https://github.com/gushmazuko/basira-vision
cd basira-vision
uv venv && uv pip install -e . pytest
source .venv/bin/activate        # Windows: .venv\Scripts\activate
basira --help
```

Run the checks the CI runs:

```bash
pytest
ruff check src tests
```

The tests need no API key and no network: a fake provider stands in for the
model. The typesetting test runs only when `pandoc` and `typst` are installed.

## Adding a language

1. Add both prompts: `src/basira_vision/prompts/ocr/<language>.txt` and
   `src/basira_vision/prompts/translate/<language>.txt` (a test requires the
   two sets to match). Copy existing ones and adapt them. Translation templates
   are written in the target language and must contain `{text}`,
   `{context_section}` and `{source_language}`; see
   [docs/prompts.md](docs/prompts.md).
2. Try it on a few real pages with `--prompts-dir` before submitting.
3. If the language needs a table-of-contents heading, add it to
   `toc_titles` in `src/basira_vision/templates/template.typ` and its
   code to `LANGUAGE_CODES` in `src/basira_vision/utils/constants.py`.
4. Mention in the pull request which material you tested on.

## Pull requests

- Branch from `main`, keep each pull request focused on one change.
- Add or update tests for behavior changes.
- Use [Conventional Commits](https://www.conventionalcommits.org/) for
  commit messages (`fix:`, `feat:`, `docs:` …).
- Update `CHANGELOG.md` under *Unreleased*.

## Releases

Maintainers bump `__version__` in `src/basira_vision/__init__.py`, move the
changelog entries under the new version and publish a GitHub release; the
release workflow builds the package and uploads it to PyPI.
