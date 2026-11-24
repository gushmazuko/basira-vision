# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.3.0] - 2026-11-24

A paper book can be read page by page with a phone camera, and every run now leaves a log file.

### Added

- Read a paper book with a phone: `basira capture mybook --source-language english` shows a QR code and an address; open it on the phone and every photo you take there is read as one page
- Open a page on the phone to check its photo and read its whole text
- Retake a page from the phone to replace it, and set the number of the next page to start anywhere or to insert a page you skipped
- Continue a book by running the same `basira capture` command again: it keeps the book's settings and reads the photos that were not read yet
- Keep a log of every run next to the file it writes, for example `workspace/mybook/mybook.log`: the command, its settings, every page, retries, failures with their reasons and the tokens used
- Turn the log file off with `--no-log`
- Show the tokens a translated page used with `--verbose`

### Changed

- Leave the text of the prompt out of what `--verbose` shows for a translated page; only its length is shown

## [0.2.4] - 2026-11-21

Single pages can be read again with different settings, and basira gives clearer guidance when something goes wrong.

### Added

- Read chosen pages again with other settings than the rest of the book, for example another prompt: `basira ocr book.pdf --page-range 100 --overwrite --custom-prompt other.txt`
- Use the built-in prompt for chosen pages of a book made with a custom prompt: `--custom-prompt ""`
- List the pages that are still failed from an earlier run at the end of `ocr`

### Changed

- Warn when pages are read again with other settings than the rest of the book
- Keep the book's language, model and other settings in the commands basira suggests after a failed or interrupted run

### Fixed

- Stop with a clear error when the header of the output file is damaged, instead of reading pages with default settings
- Say that the folder does not exist when `--prompts-dir` is mistyped
- Stop showing internal codes such as `inline:0618a7` in messages about a custom prompt
- Recognize a finished reply from gateways that write the finish reason in lowercase

## [0.2.3] - 2026-11-20

Pages the model did not finish are no longer skipped, `--resume` remembers your settings, and single pages can be read again.

### Added

- Read chosen pages again without touching the rest of the book: `basira ocr book.pdf --page-range 7-9 --overwrite`
- Save the settings of a run in the output file: language, model, prompt, `--dpi`, `--strips` and the other OCR options

### Changed

- Continue `--resume` with the settings saved in the output file, so you do not type them again
- Rename `--force-restart` to `--overwrite`; the old name still works
- Stop `--resume` when an option you type differs from the saved settings, or when there is nothing to continue
- Stop `--resume` when the saved prompt file or folder is gone, instead of quietly using the built-in prompt
- Save prompt paths without your user name, so the output file can be shared

### Fixed

- Stop skipping pages the model did not finish (blocked, cut off, or an empty reply): they are marked as failed with the reason, and `--resume` reads them again
- Mark a translated page as failed when the model returns no text for it
- Keep reading the book when several pages in a row are blocked by the model, instead of stopping the run
- Suggest the right command to continue after an interrupted `--page-range N --overwrite`
- Refuse to translate a file onto itself
- Keep the rest of the book when `--page-range` is given; `--force-restart` deleted the whole file

**Upgrading:** in files made by earlier versions a skipped page is an empty `<!-- Page N -->` block. Read it again with
`basira ocr book.pdf --page-range N --overwrite`.

## [0.2.2] - 2026-11-18

### Added

- Write the basira version that made a file into its header (`basira_version`); `--resume` keeps the version that started the file
- Document in the README that the model can rarely write its own reasoning into the page text

## [0.2.1] - 2026-11-14

### Added

- Document accuracy measurements on a Chechen book and advice on the OCR options in the README and docs/benchmark.md

### Changed

- Default to the `gemini-4-hydrogen` model (was `gemini-3.8-flash`)
- Choose the palochka's case by the spelling rules in the Chechen OCR prompt: Ӏ where a capital belongs, ӏ everywhere else, including digraphs (0.2.0 kept the printed case, which mixed the two)
- Stop suggesting 600 dpi in the `--dpi` help; use `--strips` for small print

## [0.2.0] - 2026-11-14

### Added

- Add `ocr --strips N` to send the page with N overlapping full-resolution strips of it in one request, for small letters and signs
- Add `ocr --thinking-level low|medium|high|xhigh` to set the model's reasoning effort
- Add `ocr --media-resolution low|medium|high|ultra_high` to set the token budget per image
- Log the token usage of every page (prompt, thoughts, output) with `-v`

### Changed

- Transcribe Chechen verbatim: no spelling corrections or modernization, careful ь/ъ and doubled letters, paragraphs joined and line-end hyphenation removed
- List Cyrillic І/і among the palochka lookalikes in the Chechen OCR prompt
- Use a 300 s timeout instead of 120 s for OCR requests with strips or `ultra_high`

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

[Unreleased]: https://github.com/gushmazuko/basira-vision/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/gushmazuko/basira-vision/compare/v0.2.4...v0.3.0
[0.2.4]: https://github.com/gushmazuko/basira-vision/compare/v0.2.3...v0.2.4
[0.2.3]: https://github.com/gushmazuko/basira-vision/compare/v0.2.2...v0.2.3
[0.2.2]: https://github.com/gushmazuko/basira-vision/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/gushmazuko/basira-vision/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/gushmazuko/basira-vision/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/gushmazuko/basira-vision/releases/tag/v0.1.0
