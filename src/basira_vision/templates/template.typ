// TYPST TEMPLATE FILE - A5 BOOK
// This is a Typst template file that uses Pandoc variable syntax for processing with Pandoc + Typst engine
// A5 (148×210mm), PT Serif 11pt, line step 1.35em: about 56 characters and 31 lines per page
// All layout (page size, margins, fonts, spacing) is set here; to change it,
// use your own copy: basira export template, then basira typst book.md --template template.typ
// Keep horizontalrule and divider in your copy: the converted text calls them.

// === COLOR DEFINITIONS ===
// Pure grays (luma), so print shops render them with black ink only
#let subtle-gray = luma(130)  // Subtle gray for headers and footers
#let rule-gray = luma(170)    // Thin rules in the running head

// === DOCUMENT SETUP ===
#set document(title: "$title$", author: "$author$")

// === PAGE SETUP (margins only - numbering set later per section) ===
#set page(
  paper: "a5",
  margin: (
    top: 22mm,
    bottom: 22mm,
    inside: 16mm,
    outside: 20mm
  )
)

// === TYPOGRAPHY ===
#set text(
  font: "PT Serif",
  size: 11pt,
  lang: "$if(lang)$$lang$$else$en$endif$",
  kerning: true,
  // Line box from 0.8em above to 0.2em below the baseline, so the spacing
  // below is exact for any font: line step = 1em + leading
  top-edge: 0.8em,
  bottom-edge: -0.2em
)

// Classic book paragraphs: a first-line indent and no extra space between
// paragraphs; the first paragraph after a heading is not indented
#set par(
  justify: true,
  leading: 0.35em,          // line step 1.35em (14.85pt at 11pt)
  spacing: 0.35em,          // same step between paragraphs as between lines
  first-line-indent: 1.25em
)

// === HEADING STYLES ===
// Headings are never justified or hyphenated (body text is)
#show heading: set par(justify: false)
#show heading: set text(hyphenate: false)

// The space below a heading is part of its block (not a separate v()), so
// the paragraph that follows is recognized as the first one: no indent
#show heading.where(level: 1): it => {
  pagebreak(weak: true)
  v(2em)
  block(width: 100%, below: 1.5em)[
    #align(center)[
      #text(
        size: 16pt,
        weight: "bold"
      )[#it.body]
    ]
  ]
}

#show heading.where(level: 2): it => {
  block(above: 1.4em, below: 0.8em, sticky: true)[
    #text(
      size: 12pt,
      weight: "bold"
    )[#it.body]
  ]
}

// === SCENE BREAK (*** or --- in the text) ===
// Pandoc writes #horizontalrule (older versions) or #divider() (newer ones)
#let horizontalrule = align(center, block(above: 1.2em, below: 1.2em)[\* #h(1em) \* #h(1em) \*])
#let divider() = horizontalrule

// === FOOTNOTE STYLING ===
#set footnote(numbering: "1")
// Simple styling to preserve clickable links
#show footnote.entry: set text(size: 9pt)

// === DOCUMENT STRUCTURE ===

// Front matter without page numbers
#set page(numbering: none)

// 1. TITLE PAGE
#page[
  #align(center)[
    #v(2.2cm)

    // Author
    #text(
      size: 12pt,
      style: "italic"
    )[$author$]

    #v(3.1cm)

    // Title
    #text(
      size: 20pt,
      weight: "bold"
    )[$title$]

    // Subtitle (--subtitle)
    $if(subtitle)$
    #text(
      size: 13pt,
      style: "italic",
      fill: subtle-gray
    )[$subtitle$]
    $endif$

    #v(8em)

    // Version (--version), shown as given, e.g. "Second edition"
    $if(version)$
    #text(
      size: 9.5pt,
      style: "italic",
      fill: subtle-gray
    )[$version$]

    #v(0.5em)
    $endif$

    // Year: --date value, or the current year
    #text(
      size: 10.5pt,
      style: "italic",
      fill: subtle-gray
    )[$if(date)$$date$$else$#datetime.today().year()$endif$]
  ]
]

#pagebreak(to: "odd")

// 2. TRANSLATOR PREFACE (if provided)
$if(translator_preface)$
#{
  // Keep preface headings out of the table of contents
  show heading: set heading(outlined: false)

  // Center level 2 headings in translator preface with reduced spacing
  show heading.where(level: 2): it => {
    block(width: 100%, below: 0.6em)[
      #align(center)[
        #text(
          size: 11.5pt,
          weight: "bold"
        )[#it.body]
      ]
    ]
  }

  include("$translator_preface$")
}

#pagebreak(to: "odd")
$endif$

// 3. TABLE OF CONTENTS (hidden with --no-toc)
// The heading follows the document language unless --toc-title is given
#let toc_titles = (ru: "Оглавление", fr: "Table des matières", de: "Inhalt", ar: "المحتويات")
#let doc_lang = "$if(lang)$$lang$$else$en$endif$"
#if "$show_toc$" != "false" {
  let toc_heading = if "$toc_title$" != "" { "$toc_title$" } else {
    toc_titles.at(doc_lang, default: "Contents")
  }
  heading(level: 1, outlined: false)[#toc_heading]
  v(0.75em)
  outline(title: none, indent: auto, depth: 2)
  pagebreak(to: "odd")
}

// 4. MAIN CONTENT
// Running head between two thin rules: the book title (or --header) on even
// pages, the current chapter on odd pages, nothing on a chapter's first page.
// The page number sits centered at the bottom.
#set page(
  numbering: "1",
  footer-descent: 0.75cm,
  header: context {
    counter(footnote).update(0)  // Reset footnotes per page
    let this-page = here().page()
    let chapters = query(selector(heading.where(level: 1)).after(<main-matter>))
    if not chapters.any(h => h.location().page() == this-page) {
      let earlier = chapters.filter(h => h.location().page() < this-page)
      let head = if calc.odd(counter(page).get().first()) and earlier.len() > 0 {
        earlier.last().body
      } else [$if(header)$$header$$else$$title$$endif$]
      let rule = line(length: 100%, stroke: 0.4pt + rule-gray)
      set text(size: 8.5pt, style: "italic", fill: subtle-gray)
      grid(columns: (1fr, auto, 1fr), column-gutter: 0.8em, align: horizon, rule, head, rule)
    }
  },
  footer: context [
    #set text(size: 9pt, fill: subtle-gray)
    #align(center)[#counter(page).display()]
  ]
)
#counter(page).update(1)
#metadata("main matter starts here") <main-matter>

// Page breaks in the text (one per page of the scan) never leave an empty page
#let pagebreak = pagebreak.with(weak: true)

$body$

// Even page count for printing: add one blank page (no number) if the text ends on
// an odd page. The check uses a marker placed before the blank page, so adding
// the page does not change the answer.
#metadata("end of text") <end-of-text>
#context if calc.odd(locate(<end-of-text>).page()) {
  page(header: none, footer: none)[]
}
