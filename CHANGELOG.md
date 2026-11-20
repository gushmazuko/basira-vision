# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.2.4] - 2026-11-21

Page-level `--overwrite` now accepts its own settings, and hints are more accurate.

### Changed

- Apply options typed with `ocr --page-range N --overwrite` to the replaced pages instead of stopping when they differ from the file
- Warn which settings differ in such a run, and keep the file's own settings in the frontmatter
- Suggest `--overwrite`, not `--resume`, to retry pages that failed in such a run
- Keep the file's settings in the "start over" hint when only some options are typed

### Added

- Read pages with the language prompt through `--custom-prompt ""` when the file was made with a custom prompt
- Name pages that failed in an earlier run when the current run did not touch them

### Fixed

- Stop `--resume` and page `--overwrite` when the frontmatter cannot be read, instead of continuing with default settings
- Report a `--prompts-dir` that does not exist as such on a new run
- Show an inline `--custom-prompt` as "inline text" in messages, not as a fingerprint
- Accept finish reasons from a gateway in any letter case

**Upgrading:** a page the model refuses can be read again with another prompt:
`basira ocr book.pdf --page-range 100 --overwrite --custom-prompt other.txt`

## [0.2.3] - 2026-11-20

`--overwrite` replaces `--force-restart`, `--resume` keeps the file's settings, and pages the model did not finish are retried.

### Changed

- Continue `--resume` with the settings recorded in the output file instead of the command line's defaults
- Stop `--resume` when a typed option differs from the file, or when there is no output to continue
- Stop `--resume` when the recorded prompt file or folder is missing, instead of falling back to the built-in prompt
- Record the paths of a `--custom-prompt` file and of `--prompts-dir` relative to the output file or under `~/`, so a shared file carries no user name

### Added

- Add `--overwrite` in place of `--force-restart` (still accepted): without `--page-range` it starts over, with it it replaces only those pages
- Record the settings that shape the text in the frontmatter: language, model, prompt, prompts directory, and for `ocr` also `--dpi`, `--strips`, `--media-resolution`, `--thinking-level` and `--image-format`

### Fixed

- Record a page the model did not finish (blocked, cut off at the token limit, or an empty reply) as failed with the reason in its marker, so `--resume` retries it instead of skipping it
- Fail a translation page when the model returns no text for it
- Leave such pages out of the five failures in a row that stop a run
- Continue an interrupted page `--overwrite` with `--overwrite` in the hint, since `--resume` would skip the pages not reached
- Refuse a `translate` output that would overwrite its own source
- Replace only the given pages when `--page-range` is used; `--force-restart` deleted the whole file

**Upgrading:** a page skipped by an earlier version looks like an empty `<!-- Page N -->` block. Read it again with
`basira ocr book.pdf --page-range N --overwrite`.

## [0.2.2] - 2026-11-18

### Added

- Write `basira_version` into the frontmatter of `ocr` and `translate` outputs; `--resume` keeps the version that started the file
- Document in the README that the model can rarely write its own reasoning into the page text

## [0.2.1] - 2026-11-14

### Changed

- Default to the `gemini-4-hydrogen` model (was `gemini-3.8-flash`)
- Choose the palochka's case by the spelling rules in the Chechen OCR prompt: Ӏ where a capital belongs, ӏ everywhere else, including digraphs (0.2.0 kept the printed case, which mixed the two)
- Stop suggesting 600 dpi in the `--dpi` help; use `--strips` for small print

### Added

- Document accuracy measurements on a Chechen book and advice on the OCR options in the README and docs/benchmark.md

## [0.2.0] - 2026-11-14

### Changed

- Transcribe Chechen verbatim: no spelling corrections or modernization, careful ь/ъ and doubled letters, paragraphs joined and line-end hyphenation removed
- List Cyrillic І/і among the palochka lookalikes in the Chechen OCR prompt
- Use a 300 s timeout instead of 120 s for OCR requests with strips or `ultra_high`

### Added

- Add `ocr --strips N` to send the page with N overlapping full-resolution strips of it in one request, for small letters and signs
- Add `ocr --thinking-level low|medium|high|xhigh` to set the model's reasoning effort
- Add `ocr --media-resolution low|medium|high|ultra_high` to set the token budget per image
- Log the token usage of every page (prompt, thoughts, output) with `-v`

## [0.1.0] - 2026-11-09

First public release.

### Added

- Add `basira ocr` and `basira translate` for Arabic, Chechen, English, French, German and Russian: vision OCR of scanned PDFs, and page-by-page translation with the previous page as context
- Add `basira typst`: A5 book typesetting with Pandoc and an editable Typst template that holds all the layout; `--template` uses your own copy
- Keep the page count of the typeset book even by adding a blank last page when needed
- Add `basira export template` and `basira export prompts` to write editable copies of the built-in template and prompts
- Add resumable runs: `--resume` processes only missing or failed pages, so chunked `--page-range` runs and retries work
- Never replace an existing output without `--resume` or `--force-restart`
- Add a live terminal view with a page-map progress bar, ETA, retry and rate-limit countdowns and a summary that names the next command; plain logfmt lines when not on a terminal; `--log-format json`
- Finish the current page on Ctrl-C before stopping; stop at once on a second Ctrl-C
- Stop a run after 5 failed pages in a row, and report an unreachable endpoint or a rejected key before the first page
- Keep the reason of a failed page in its marker; failed pages never reach the typeset book, and `typst` names them in a warning
- Add exit codes: 0 done, 3 done with failed pages, 1 error, 2 usage, 130 paused
- Add custom prompt directories (`--prompts-dir`, `BASIRA_PROMPTS_DIR`)
- Add Gemini-protocol gateways through `GEMINI_BASE_URL`

[Unreleased]: https://github.com/gushmazuko/basira-vision/compare/v0.2.4...HEAD
[0.2.4]: https://github.com/gushmazuko/basira-vision/compare/v0.2.3...v0.2.4
[0.2.3]: https://github.com/gushmazuko/basira-vision/compare/v0.2.2...v0.2.3
[0.2.2]: https://github.com/gushmazuko/basira-vision/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/gushmazuko/basira-vision/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/gushmazuko/basira-vision/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/gushmazuko/basira-vision/releases/tag/v0.1.0
