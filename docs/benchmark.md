# Accuracy benchmark

How accurately does `basira ocr` read a hard scan, and which options matter?
This page describes the measurement behind the [Accuracy](../README.md#accuracy)
section of the README and the advice in "Getting the most accurate text".

## Material

Eight scanned pages of one printed book in **Chechen**, a low-resource language
written in Cyrillic with the palochka (Ӏ), a letter that looks like a plain
vertical bar and that most OCR engines misread. The pages were chosen for
difficult layouts:

- prose with bold and italic words and footnote marks;
- a dictionary page that mixes Chechen and Russian, with arrows and abbreviations;
- a captioned illustration;
- a four-column table and a numbered list.

2,444 words in total. The scan has no text layer; it is rendered at 300 dpi.
The pages themselves are not published.

## Reference transcription

The reference text was made by proofreading model output letter by letter
against the scan, zooming in on every doubtful word. Every hard spot where
engines disagreed with that reading was then checked by a native speaker
against the printed book. The reference keeps the book exactly as printed,
including its own typos and the spelling used before the 2020 Chechen
spelling reform.

Bias: because the reference started from model output, it may favor model-like
readings in places nobody flagged as doubtful.

## Scoring

- Word-level comparison of the output with the reference, in reading order.
- The text is compared **as it comes out of each engine**: no symbol is
  replaced (a Latin `I` written for the palochka is an error).
- Only formatting is aligned, the same way for every engine: markdown marks,
  printed page numbers, the space before a footnote mark, and line-end
  hyphenation (joined for engines that keep printed line breaks).
- The palochka's case (Ӏ or ӏ) is not scored: print does not show it.
  basira chooses it by the spelling rules.
- The result is **wrong words per 1,000 words**. Vision models do not give the
  same answer twice, so every basira setup was run 5 times; the table shows
  the mean and the range.

## Results

basira 0.2.1, model `gemini-4-hydrogen`, model's default thinking level.

| Setup | Wrong words per 1,000 | Range over 5 runs |
|---|---:|---:|
| EasyOCR 1.7.2, `["che"]` | 182.9 | – |
| PaddleOCR 3.7.0, `lang="che"` | 134.2 | – |
| basira, page only | 3.9 | 2.9–5.7 |
| basira, `--strips 3` | 2.9 | 1.6–4.1 |

EasyOCR and PaddleOCR officially list Chechen (`che`, through their shared
Cyrillic models) and were run with their default settings, once each (their
output does not change between runs). PaddleOCR ran on paddlepaddle 3.2.2
with the PP-OCRv5 models of PaddleOCR 3.7.0.

Most of the errors that remain with basira are not misread letters but words
silently changed to a more common form: a rare word replaced by a similar
familiar one, an unusual ending normalized, or a typo of the book "fixed".
Review the text with that in mind.

## What the option advice is based on

Measured on the same pages with `gemini-4-hydrogen` during the development of
0.2.0 (a development version of the Chechen prompt, so the absolute numbers
differ slightly from the table above).

- **`--strips`.** The table above shows the gain at the default thinking
  level (3.9 to 2.9 per 1,000). On two of the pages with 10 runs each,
  3 strips averaged 1.2 wrong words, 5 strips 1.4 and 8 strips 1.9: more strips
  did not help and made the model "correct" printed words more often.
- **`--thinking-level`.** Over all eight pages, 20 runs per level (4 prompt
  variants x 5 runs), `low` averaged 12.7 wrong words per run against 5.4 at
  the model's default level, and in 4 of 20 runs it wrote a whole page with
  lookalike Unicode characters (a decomposed й, or Cyrillic І for the
  palochka). `xhigh` was not more accurate than the default on the pages it
  was run on.
- **`--media-resolution ultra_high`.** 5 runs each on all eight pages: 4.6
  against 5.6 wrong words per run, a difference within run-to-run noise. It
  fixed some hard words but added new misread letters elsewhere, such as an
  extra or a wrong letter in a clearly printed word.
- **The prompt.** Removing single rules or rewriting the Chechen prompt from
  scratch did not reduce the total; two added prohibitions made it worse.

## Limits

- One book, one language, about 2,400 words. Other books, scripts and models
  will give different numbers; treat these as an indication of scale.
- The reference transcription was not produced independently of model output
  (see above).
- The test pages cannot be published, so the measurement cannot be repeated
  by others as is.
