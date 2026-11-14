# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.2.1] - 2026-11-14

- The default model is now `gemini-4-hydrogen` (was `gemini-3.8-flash`).
- Chechen OCR prompt: the palochka's case follows the spelling rules, since
  print does not show it: Ӏ where a capital belongs (start of a sentence or a
  proper name, words in capitals), ӏ everywhere else, including digraphs.
  0.2.0 asked the model to keep the printed case, which made it mix Ӏ and ӏ.
- `--dpi` help no longer suggests 600 dpi; use `--strips` for small print.
- README: accuracy measurements on a Chechen book and advice on the OCR
  options, without speed or cost claims; full method in docs/benchmark.md.

## [0.2.0] - 2026-11-14

- `basira ocr --strips N`: sends the page together with N overlapping
  full-resolution strips of it in one request, for small letters and signs.
- `basira ocr --thinking-level low|medium|high|xhigh` sets the model's
  reasoning effort; without it the model uses its own default.
- `basira ocr --media-resolution low|medium|high|ultra_high` sets the token
  budget per image.
- OCR requests with strips or `ultra_high` use a 300 s timeout instead of 120 s.
- With `-v`, the token usage of every page is logged (prompt, thoughts, output).
- Chechen OCR prompt: verbatim transcription (no spelling corrections or
  modernization, careful ь/ъ and doubled letters), Cyrillic І/і listed
  among the palochka lookalikes, paragraphs joined and line-end hyphenation
  removed. The palochka keeps its printed case, as before.

## [0.1.0] - 2026-11-09

First public release.

- `basira ocr` and `basira translate` for Arabic, Chechen, English, French,
  German and Russian: vision OCR of scanned PDFs, and page-by-page
  translation with the previous page as context.
- `basira typst`: A5 book typesetting with Pandoc and an editable Typst
  template that holds all the layout; `--template` uses your own copy.
  The page count is always even: a blank last page is added when needed.
- `basira export template` and `basira export prompts` write copies of the
  built-in template and prompts to edit.
- Resumable runs: `--resume` processes only missing or failed pages, so
  chunked `--page-range` runs and retries work. Existing output is never
  replaced without `--resume` or `--force-restart`.
- Terminal output: a live view with a page-map progress bar, ETA, retry and
  rate-limit countdowns and a summary that names the next command; plain
  logfmt lines when not on a terminal; `--log-format json`.
- Ctrl-C finishes the current page before stopping; a second Ctrl-C stops
  at once. A run stops after 5 failed pages in a row, and an unreachable
  endpoint or rejected key is reported before the first page.
- Failed pages keep their reason in the page marker and never reach the
  typeset book; `typst` names them in a warning.
- Exit codes: 0 done, 3 done with failed pages, 1 error, 2 usage, 130 paused.
- Custom prompt directories (`--prompts-dir`, `BASIRA_PROMPTS_DIR`).
- Gemini-protocol gateways via `GEMINI_BASE_URL`.

[Unreleased]: https://github.com/gushmazuko/basira-vision/compare/v0.2.1...HEAD
[0.2.1]: https://github.com/gushmazuko/basira-vision/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/gushmazuko/basira-vision/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/gushmazuko/basira-vision/releases/tag/v0.1.0
