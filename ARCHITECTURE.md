# Architecture

Basira Vision is a three-stage command-line pipeline. Each stage reads and
writes plain files, so a person can review and edit the result before the
next stage runs.

```
scan.pdf ──ocr──▶ book.md ──translate──▶ book_<lang>.md ──typst──▶ book_<lang>.pdf
            │                  │                            │
     page image + prompt   page + previous page's     pandoc + Typst
     → vision model        translation → model        template
```

## Modules

```
src/basira_vision/
├── cli.py                     click commands: arguments, checks before a run, summaries
├── pipeline.py                the page loop shared by ocr and translate
├── settings.py                environment / .env configuration (pydantic-settings)
├── ui/                        terminal output (see "Output" below)
│   ├── events.py              what the pipeline reports
│   ├── state.py               progress, timing and the ETA
│   ├── live.py                interactive view for terminals
│   └── plain.py               logfmt and JSON lines for logs and scripts
├── providers/
│   ├── base.py                VisionProvider interface: extract_text, generate_text, check_connection
│   └── gemini.py              Gemini-protocol client: Google API or a compatible gateway,
│                              retries on 408/429/5xx and transport errors, honors Retry-After,
│                              announces every retry through on_retry
├── processing/
│   ├── pdf_converter.py       page count, --page-range parsing, page rendering (PyMuPDF)
│   ├── prompt_loader.py       prompt lookup: user --prompts-dir first, then built-ins
│   ├── translation.py         prompt rendering, sliding-window context, output naming
│   └── typst_processor.py     frontmatter + CLI metadata, pandoc/typst invocation
├── utils/
│   ├── pages.py               page files (see below)
│   ├── constants.py, errors.py, logging.py
├── prompts/{ocr,translate}/   prompt templates (see docs/prompts.md)
└── templates/template.typ   Pandoc template for Typst: the whole A5 book layout
```

## Page files

The OCR output and the translation share one format: YAML frontmatter, then
one block per page, each starting with its original page number.

```markdown
---
title: ""
author: ""
source_language: "english"
processing_status: "Completed"
---

<!-- Page 1 -->
First page text

---

<!-- Page 2 - Error: HTTP 500 INTERNAL -->
```

`utils/pages.py` keeps the pages of a file in a dictionary keyed by page
number and rewrites the whole file atomically (temporary file + rename) after
every page. That gives the resume behavior:

- an interrupted run loses at most the page in flight;
- `--resume` processes only pages that are missing or marked `- Error`, so
  chunked runs (`--page-range 1-50`, then `--page-range 51-100 --resume`)
  and retries of failed pages both work;
- pages are always written in page order, whatever order they were
  processed in;
- only the frontmatter is edited when the status changes.

A failed page keeps its reason inside the marker and has no body. Pandoc
drops HTML comments, so a failure never reaches the typeset book. Text
written under an error marker by hand makes the page count as done.

Translations separate pages with a raw Typst page break instead of `---`, so
the typeset book keeps the page structure of the source.

## Sliding-window translation

Pages are translated in order. The prompt for page *N* contains the finished
translation of the page before it, read back from the output file, so the
window also survives a resume. If that page failed, or the source page itself
has no OCR text, the context is empty: one bad page cannot leak into the
next.

## Typesetting

`typst` resolves title, author and language from the frontmatter (CLI options
win), converts an optional translator's preface to Typst with pandoc, and
renders the book with `pandoc --pdf-engine=typst` and the template. The
template is plain Typst with Pandoc variables; page size, fonts and structure
are meant to be edited.

## The page loop

`pipeline.run_pages` runs the pages of `ocr` and `translate`:

- every finished or failed page is written to the page file at once;
- a failed page is recorded and the run continues; after 5 failures in a
  row the run stops, because the endpoint is most likely down;
- authentication errors and exhausted rate limits stop the run right away:
  every following page would fail the same way, and the current page is
  simply left missing for `--resume`;
- the first Ctrl-C lets the current page finish and then stops; the second
  one stops immediately.

Before the loop, the CLI checks what can be checked cheaply: an existing
output is never replaced without `--resume` or `--force-restart`, and the
endpoint is asked for the model with a short timeout, so a wrong URL or key
fails before the first page.

## Output

The pipeline never prints. It emits events (`ui/events.py`: run started,
page started, retrying, page done or failed, run finished, …) to a
`Reporter`, which hands them to one renderer:

- **live** (stderr is a terminal): a header, a pinned status block (a bar
  whose cells map the pages of the book, counts, elapsed time and ETA, and
  the current activity with retry countdowns), failures printed above it and
  collapsed when they repeat, and a summary with the exact command to run
  next;
- **plain** (anything else): one timestamped logfmt line per event;
- **json** (`--log-format json`): one JSON object per event.

Python `logging` is routed into the same reporter, so third-party warnings
and `--verbose` debug lines appear in the same place and format. Everything
goes to stderr; stdout carries only the path of the result, so commands can
be chained.

Exit codes: 0 done, 3 done with failed pages, 1 error or stopped, 2 invalid
usage, 130 paused with Ctrl-C.

## Tests

`tests/` runs without network access: a fake provider replaces Gemini, PDFs
are generated on the fly, and the typesetting tests run only when pandoc and
typst are installed. The plain renderer is deterministic, so most CLI tests
assert on its lines; the live renderer is tested against a recording
console.
