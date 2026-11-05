# Prompts

Both model-driven stages are steered by plain-text prompt templates, one file
per language:

```
src/basira_vision/prompts/
├── ocr/          # file name = language of the scan      (--source-language)
│   ├── arabic.txt     right-to-left reading order, columns, diacritics, numerals
│   ├── chechen.txt    Cyrillic palochka (Ӏ/ӏ) and digraphs
│   ├── english.txt    baseline
│   ├── french.txt     accents, French punctuation
│   ├── german.txt     umlauts and ß, „…“ quotes, words hyphenated at line ends, Fraktur
│   └── russian.txt    ё, Russian quotes and dashes
└── translate/    # file name = language of the translation (--target-language)
    ├── arabic.txt
    ├── chechen.txt    instructions in English (see below)
    ├── english.txt
    ├── french.txt
    ├── german.txt
    └── russian.txt
```

Every language has both an OCR and a translation prompt (a test checks it).

Selection is strict: an unknown language exits with an error that lists the
available ones, there is no silent fallback.

## Your own prompts

You do not need to touch the installed package. `basira export prompts`
writes a copy of all built-in prompts to `./prompts` (or a folder you name);
edit them there and point `--prompts-dir` (or the `BASIRA_PROMPTS_DIR`
environment variable) at that folder. A directory can also hold only the
files you want to change, with the same layout:

```
my-prompts/
├── ocr/greek.txt          # adds --source-language greek
└── translate/english.txt  # replaces the built-in English translation prompt
```

Files there are searched before the built-in ones, so the directory can both
add languages and override shipped ones.

For a one-off run, `--custom-prompt` takes either the prompt text or a path to
a prompt file.

## OCR templates

Plain instructions with no placeholders: the page image is sent together with
the text. Add a language when it needs special handling (script direction,
diacritics, letters that models confuse, numerals); otherwise the `english`
prompt works for most Latin-script books.

All shipped OCR prompts tell the model to drop printed page numbers, running
headers and footers (the typesetting stage adds its own), to mark chapter
titles with `#` and section titles with `##`.

## Translation templates

Placeholders, substituted literally (other braces in the text are left alone,
no escaping needed):

| Placeholder | Receives |
|-------------|----------|
| `{text}` | the OCR text of the current page (**required**) |
| `{context_section}` | the finished translation of the previous page, empty on the first page and after a failed page |
| `{source_language}` | `source_language` from the OCR file's frontmatter, or `Auto` |

Templates are source-language neutral and written in the target language,
except `chechen.txt`: its instructions are in English, like `ocr/chechen.txt`,
because models follow English instructions more reliably for a low-resource
language; the Chechen-specific rules (Cyrillic only, the palochka Ӏ/ӏ as its
own letter, digraphs) are spelled out in it.

The shipped ones share one structure: role, core requirements, formatting rules,
key principles, unreadable-text policy, tone, then the context block (with an
instruction not to re-translate it) and the source text block, followed by an
"output only the translation" instruction. Copy one of them when adding a
language.

The context block is what makes the sliding window work: the model sees how
the previous page was translated and continues with the same names, terms and
voice.

## Tips

- Test a new prompt on a few representative pages (`--page-range 10-12`)
  before running a whole book.
- Output quality depends on the model as much as on the prompt; a stronger
  model often helps more than a longer prompt.
- Contributions of new languages are welcome; see
  [CONTRIBUTING.md](../CONTRIBUTING.md).
