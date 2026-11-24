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

`capture` is a second way to get `book.md`: instead of a scanned PDF, the
pages are photographed with a phone.

## Modules

```
src/basira_vision/
├── cli.py                     click commands: arguments, checks before a run, summaries
├── pipeline.py                the page loop shared by ocr and translate
├── capture/                   pages from a phone camera (see "Capture" below)
│   ├── session.py             photos, the reading queue, the page file; no network
│   ├── server.py              standard-library HTTP server: the page, uploads, status
│   └── page.html              the page the phone opens, no framework
├── settings.py                environment / .env configuration (pydantic-settings)
├── ui/                        terminal output (see "Output" below)
│   ├── events.py              what the pipeline reports
│   ├── journal.py             the log file of a run (see "The log file" below)
│   ├── qr.py                  a small QR code encoder, for the address `capture` shows
│   ├── state.py               progress, timing and the ETA
│   ├── live.py                interactive view for terminals
│   └── plain.py               logfmt and JSON lines for logs and scripts
├── providers/
│   ├── base.py                VisionProvider interface: extract_text, generate_text, check_connection
│   └── gemini.py              Gemini-protocol client: Google API or a compatible gateway,
│                              retries on 408/429/5xx and transport errors, honors Retry-After,
│                              announces every retry through on_retry; for OCR also page strips,
│                              media resolution and thinking level, 300 s timeout for heavy requests
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
- only the frontmatter is edited when the status changes;
- the settings that shape the text are recorded in the frontmatter when the
  file is created, and `cli.py` reuses them on `--resume` and on an
  `--overwrite` of only some pages (`_resume_settings`): an option the user
  did not type comes from the file. A typed one that differs stops a
  `--resume`, so a file is never continued with another configuration by
  accident. On an `--overwrite` of some pages it is a deliberate choice for
  those pages: it is used, the run warns which settings differ, and the
  frontmatter keeps the file's own. An `--overwrite` of the whole book (no
  `--page-range`, or one that covers every page) starts a new file and may
  use new settings;
- a model reply that is not a finished one (cut off, blocked, empty with no
  finish reason; `providers/gemini.py::_reply_text`) raises
  `UnfinishedReplyError`, so the page is recorded as failed with the reason
  and `--resume` retries it. It is a problem of that page, so it does not
  count toward the consecutive failures that stop a run. A reply that
  finished normally with no text is a blank page.

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
output is never replaced without `--resume` or `--overwrite`, and the
endpoint is asked for the model with a short timeout, so a wrong URL or key
fails before the first page.

## Capture

`basira capture NAME` writes the same page file as `ocr`, but the pages come
from a phone: it serves one HTML page on the local network, and every photo
taken there is one page. There is no list of pages to loop over, so it does
not use `run_pages`.

- **The phone side** is a plain file input (`accept="image/*"
  capture="environment"`): the browser opens the phone's own camera and
  uploads the full-resolution photo, and this works over plain HTTP. The
  page polls a JSON status and shows each page's state; an opened page
  gets its whole text and a larger rendering of its photo on request. The
  camera only opens from a tap, so nothing on the way to it asks a
  question in a dialog: the shutter button itself says "Retake page N"
  when the number is taken.
- **The session** (`capture/session.py`) saves an upload as
  `images/page_NNN.jpg`, turned upright from its EXIF orientation, and puts
  the page in a queue. It takes a JPEG or a PNG and nothing else; any other
  file gets one answer. (Taking whatever Pillow can open is not harmless: a
  RAW photo is a TIFF that starts with a small preview, and the preview
  would be read as the page.) The JPEG it writes carries none of the
  upload's metadata. One worker thread reads the queue with the same
  provider call, prompt and settings as `ocr`. HTTP handler threads save
  photos and read snapshots; every use of the page file, from either side,
  happens under one lock, and events are reported under it too, so they
  come out in the order things happened.
- **Nothing extra is stored about the queue.** What is still to read is
  every photo whose page is missing from the page file or failed in it. So
  running the command again reads those photos, the way `--resume` retries
  pages. A retake removes the page's old text at once for the same reason:
  the new photo then counts as not read until it is.
- **Settings** are recorded in the frontmatter as `ocr` does, without `dpi`
  and `image_format`, which say nothing about a photo. An existing book is
  always continued with its recorded settings; a typed option that differs
  stops the command. `ocr --resume` on such a file finds no conflict. A
  file with only a header and no photo (a session that ended before its
  first photo) is not a book yet and is written anew. A book that `ocr`
  left `Incomplete` keeps that status: only `ocr` knows the pages of its
  PDF.
- **Failures** follow the page loop's rules: a failed page is recorded with
  its reason and can be retaken; a rejected key, an exhausted rate limit or
  5 failures in a row stop the reading, while photos keep being accepted
  and wait for the next run.
- **Ending.** The first Ctrl-C lets the page in progress finish; from that
  moment no photo is taken any more (the phone is told so), because it
  would be left unread without a word. A second Ctrl-C gives the page in
  progress up: its result is thrown away when it arrives, so nothing is
  written after the summary.
- **Access.** Every path is below a random key that is new on each start
  (`/<key>/`); anything else answers 404, whatever the method. That is all
  the protection there is: no TLS and no accounts, by design, for use on
  one's own network. A request the server cannot make sense of gets the
  same 404.

The session reports through the same events as the page loop, plus a few of
its own (`ui/events.py`): `CaptureStarted` carries the address,
`PhotoReceived` a new photo, `PageRead` the beginning of a page's text,
`ReadingStopped` the reason reading stopped. `CaptureStarted` and `PageRead`
extend `RunStarted` and `PageDone`: a renderer without a handler of its own
treats them as those, and the JSON output keeps the common event names.
With no total number of pages there is no page map, so the live view shows
the address as a QR code (`ui/qr.py`: byte mode, level L, versions 1 to 5,
enough for a URL and written here to avoid a dependency), counts of pages
read, waiting and failed, and a line per finished page.

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

### The log file

The reporter has a second place to report to, next to the terminal: the
journal of the run (`ui/journal.py`). It gets every event and formats it with
the plain renderer, so the log has the same lines whatever the terminal
shows, and it is appended to `<output>.log` next to the file the command
writes (`book.log` for `ocr` and `capture`, `book_russian.log` for
`translate` and `typst`).

- **When it starts.** A command names the log as soon as it knows its output
  (`Reporter.log_to`) and starts it once it has created that output
  (`Reporter.start_log`). Until then the lines are kept in memory, with their
  times. So a first run that fails before it creates anything (no key, the
  endpoint down) leaves no log behind, and nothing of a run that does start
  is missing. If the output or its log exists already, the log is written
  at once: a run that is refused ("already exists", "was made with other
  settings") is in the book's log.
- **How a line is written.** One `os.write` per line on a descriptor opened
  for appending. A line is on disk when it is written, two commands on one
  book do not cut each other's lines, and there is no buffer that could be
  caught half-written: the first Ctrl-C of `ocr` and `translate` is reported
  from the signal handler, which runs wherever the main thread happens to
  be, possibly inside another line.
- **Levels.** The terminal has two: warnings, and details shown with
  `--verbose`. The log has three: warnings and info records are always
  kept, debug records only with `--verbose`. Info is for what is worth
  keeping and holds no page text: the tokens a request used, an upload that
  was refused. Debug can be long (in `capture`, one line per request of the
  phone).
- **What is never in it.** The text of the pages (the plain renderer does
  not print a page's preview, and a prompt is logged by its length only),
  the API key, and the key in the address of a `capture` session, which is
  written as `<key>`.
- **Failure.** A log must not stop the run it is the log of: if the file
  cannot be opened or written, the terminal gets one warning and the run
  goes on without it. An error nobody planned for is added to the log
  before it ends the run.

Exit codes: 0 done, 3 done with failed pages, 1 error or stopped, 2 invalid
usage, 130 paused with Ctrl-C.

## Tests

`tests/` runs without network access: a fake provider replaces Gemini, PDFs
are generated on the fly, the capture session is driven directly and its
HTTP layer on a free local port, the script of the phone page runs against
a fake DOM when node is installed, and the typesetting tests run only when pandoc and
typst are installed. The plain renderer is deterministic, so most CLI tests
assert on its lines; the live renderer is tested against a recording
console.
