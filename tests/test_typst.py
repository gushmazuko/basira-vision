import subprocess

import pymupdf
import pytest
from click.testing import CliRunner

from basira_vision.cli import main
from basira_vision.processing import typst_processor
from basira_vision.processing.typst_processor import resolve_metadata


def test_cli_overrides_frontmatter():
    content = '---\ntitle: "From YAML"\nauthor: ["A", "B"]\ntarget_language: "russian"\n---\n\nbody'
    assert resolve_metadata(content) == {
        "title": "From YAML", "authors": ["A", "B"], "translator_preface": None, "lang": "ru",
    }
    meta = resolve_metadata(content, title="CLI", author="C", lang="en")
    assert (meta["title"], meta["authors"], meta["lang"]) == ("CLI", ["C"], "en")


def test_missing_metadata_is_reported(tmp_path):
    source = tmp_path / "book.md"
    source.write_text('---\ntitle: ""\nauthor: ""\n---\n\nbody\n')
    with pytest.raises(typst_processor.TypesettingError, match="title and author"):
        typst_processor.generate_pdf(source, tmp_path / "book.pdf")


@pytest.mark.skipif(typst_processor.missing_tools(), reason="pandoc and typst are required")
@pytest.mark.parametrize("toc", [True, False])
def test_typesets_a_translation(tmp_path, toc):
    source = tmp_path / "book_russian.md"
    source.write_text(
        '---\ntitle: "Книга"\nauthor: "Автор"\ntarget_language: "russian"\n---\n\n'
        "<!-- Page 1 -->\n# Глава 1\n\nПервая страница.\n\n```{=typst}\n#pagebreak()\n```\n\n"
        "<!-- Page 2 -->\nВторая страница.\n"
    )
    preface = tmp_path / "preface.md"
    preface.write_text("## От переводчика\n\nТекст.\n")

    flag = "--toc" if toc else "--no-toc"
    result = CliRunner().invoke(main, ["typst", str(source), "--translator-preface", str(preface), flag])
    assert result.exit_code == 0, result.output

    with pymupdf.open(tmp_path / "book_russian.pdf") as doc:
        text = "".join(page.get_text() for page in doc)
    assert "Книга" in text and "Вторая страница" in text and "От переводчика" in text
    assert ("Оглавление" in text) is toc
    assert not list(tmp_path.glob("*.typ"))  # the converted preface is cleaned up


@pytest.mark.skipif(typst_processor.missing_tools(), reason="pandoc and typst are required")
def test_failed_pages_stay_out_of_the_book_with_a_warning(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "book_russian.md"
    source.write_text(
        '---\ntitle: "Книга"\nauthor: "Автор"\nsource_file: "book.md"\ntarget_language: "russian"\n'
        'processing_status: "Incomplete"\n---\n\n'
        "<!-- Page 1 -->\nПервая страница.\n\n```{=typst}\n#pagebreak()\n```\n\n"
        "<!-- Page 2 - Error: HTTP 500 INTERNAL: simulated -->\n\n```{=typst}\n#pagebreak()\n```\n\n"
        "<!-- Page 3 -->\nТретья страница.\n"
    )
    result = CliRunner().invoke(main, ["typst", "book_russian.md"])
    assert result.exit_code == 0, result.output
    assert "page 2 failed in an earlier step and is left out of the book" in result.stderr
    assert "basira translate book.md --target-language russian --resume" in result.stderr
    assert result.stdout == "book_russian.pdf\n"

    with pymupdf.open(tmp_path / "book_russian.pdf") as doc:
        text = "".join(page.get_text() for page in doc)
    assert "Третья страница" in text
    assert "HTTP 500" not in text and "Error" not in text


needs_tools = pytest.mark.skipif(typst_processor.missing_tools(), reason="pandoc and typst are required")

CHAPTER = (
    '---\ntitle: "Книга"\nauthor: "Автор"\ntarget_language: "russian"\n---\n\n'
    "<!-- Page 1 -->\n# Глава I\n\nПервая страница.\n\n***\n\nПосле разрыва сцены.\n\n"
    "```{=typst}\n#pagebreak()\n```\n\n<!-- Page 2 - Error: HTTP 500 INTERNAL -->\n\n"
    "```{=typst}\n#pagebreak()\n```\n\n<!-- Page 3 -->\nТретья страница.\n"
)


def typeset(tmp_path, monkeypatch, *args):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "book.md").write_text(CHAPTER)
    result = CliRunner().invoke(main, ["typst", "book.md", *args])
    assert result.exit_code == 0, result.output
    return pymupdf.open(tmp_path / "book.pdf")


@needs_tools
def test_contents_are_shown_by_default_and_hidden_with_no_toc(tmp_path, monkeypatch):
    with typeset(tmp_path, monkeypatch) as doc:
        assert any("Оглавление" in p.get_text() for p in doc)
    with typeset(tmp_path, monkeypatch, "--no-toc") as doc:
        assert not any("Оглавление" in p.get_text() for p in doc)


@needs_tools
def test_scene_break_and_no_empty_page_for_a_failed_page(tmp_path, monkeypatch):
    with typeset(tmp_path, monkeypatch, "--no-toc") as doc:
        pages = [p.get_text() for p in doc]
    chapter = next(i for i, text in enumerate(pages) if "Первая страница" in text)
    assert "*" in pages[chapter] and "После разрыва сцены" in pages[chapter]
    assert "Третья страница" in pages[chapter + 1]  # page 2 failed and left no empty page


def test_layout_lives_in_the_template_only(tmp_path):
    for flag in ("--paper", "--size", "--leading", "--margin-top", "--mainfont"):
        result = CliRunner().invoke(main, ["typst", str(tmp_path), flag, "x"])
        assert result.exit_code == 2 and "No such option" in result.output


@needs_tools
@pytest.mark.parametrize("chapters", [1, 2])
def test_page_count_is_always_even(tmp_path, monkeypatch, chapters):
    monkeypatch.chdir(tmp_path)
    body = "".join(f"# Глава {n}\n\nТекст главы.\n\n" for n in range(chapters))
    (tmp_path / "book.md").write_text(f'---\ntitle: "Книга"\nauthor: "Автор"\n---\n\n{body}')
    assert CliRunner().invoke(main, ["typst", "book.md", "--no-toc"]).exit_code == 0
    with pymupdf.open(tmp_path / "book.pdf") as doc:
        pages = [p.get_text() for p in doc]
    assert len(pages) % 2 == 0
    assert "Текст главы" in pages[-1] or pages[-1].strip() == ""


def pt_serif_installed():
    return not typst_processor.missing_tools() and "PT Serif" in subprocess.run(
        ["typst", "fonts"], capture_output=True, text=True).stdout


@needs_tools
@pytest.mark.skipif(not pt_serif_installed(), reason="the PT Serif font is not installed")
def test_body_text_is_set_in_pt_serif(tmp_path, monkeypatch):
    with typeset(tmp_path, monkeypatch, "--no-toc") as doc:
        fonts = {font[3] for page in doc for font in page.get_fonts()}
    assert any("PTSerif" in name for name in fonts), fonts


@needs_tools
def test_custom_template(tmp_path, monkeypatch):
    custom = typst_processor.TEMPLATE_PATH.read_text().replace('ru: "Оглавление"', 'ru: "Содержание"')
    (tmp_path / "my.typ").write_text(custom)
    with typeset(tmp_path, monkeypatch, "--template", "my.typ") as doc:
        assert any("Содержание" in p.get_text() for p in doc)


@needs_tools
def test_a5_page_and_running_heads(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    long_text = "\n\n".join(["Длинный абзац текста книги, который повторяется много раз. " * 6] * 30)
    (tmp_path / "book.md").write_text(
        '---\ntitle: "Книга"\nauthor: "Автор"\ntarget_language: "russian"\n---\n\n'
        f"# Первая глава\n\n{long_text}\n"
    )
    assert CliRunner().invoke(main, ["typst", "book.md", "--no-toc"]).exit_code == 0
    with pymupdf.open(tmp_path / "book.pdf") as doc:
        assert (round(doc[0].rect.width / 72 * 25.4), round(doc[0].rect.height / 72 * 25.4)) == (148, 210)
        body = [p for p in doc if "Длинный абзац" in p.get_text()]
        first_lines = [p.get_text().split("\n", 1)[0] for p in body[:3]]
    assert first_lines[0] == "Первая глава"   # chapter opener: no running head
    assert first_lines[1] == "Книга"          # page 2 (even): the book title
    assert first_lines[2] == "Первая глава"   # page 3 (odd): the current chapter


@needs_tools
def test_frontmatter_preface_path_is_relative_to_the_book(tmp_path, monkeypatch):
    book_dir = tmp_path / "books" / "novel"
    book_dir.mkdir(parents=True)
    (book_dir / "preface.md").write_text("# Предисловие\n\nТекст предисловия.\n")
    (book_dir / "book.md").write_text(
        '---\ntitle: "Книга"\nauthor: "Автор"\ntranslator_preface: preface.md\n---\n\n# Глава\n\nТекст.\n'
    )
    monkeypatch.chdir(tmp_path)  # run from another folder
    result = CliRunner().invoke(main, ["typst", "books/novel/book.md", "--no-toc"])
    assert result.exit_code == 0, result.output
    with pymupdf.open(book_dir / "book.pdf") as doc:
        assert any("Текст предисловия" in p.get_text() for p in doc)


def test_export_template_and_prompts(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(main, ["export", "template"])
    assert result.exit_code == 0 and result.stdout == "template.typ\n"
    assert (tmp_path / "template.typ").read_text() == typst_processor.TEMPLATE_PATH.read_text()

    result = CliRunner().invoke(main, ["export", "prompts", "my-prompts"])
    assert result.exit_code == 0
    folder = tmp_path / "my-prompts"
    exported = sorted(p.relative_to(folder).as_posix() for p in folder.rglob("*.txt"))
    assert len(exported) == 12 and "ocr/arabic.txt" in exported and "translate/russian.txt" in exported

    (tmp_path / "template.typ").write_text("mine")
    result = CliRunner().invoke(main, ["export", "template"])
    assert result.exit_code == 1 and "already exists" in result.stderr
    assert (tmp_path / "template.typ").read_text() == "mine"
    assert CliRunner().invoke(main, ["export", "prompts", "my-prompts"]).exit_code == 1
    assert CliRunner().invoke(main, ["export", "template", "--force"]).exit_code == 0
    assert (tmp_path / "template.typ").read_text() != "mine"


@needs_tools
def test_classic_paragraphs_first_one_after_a_heading_is_not_indented(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "book.md").write_text(
        '---\ntitle: "Книга"\nauthor: "Автор"\n---\n\n# Глава\n\nПервый абзац.\n\nВторой абзац.\n\n'
        "## Раздел\n\nТретий абзац.\n\nЧетвёртый абзац.\n"
    )
    assert CliRunner().invoke(main, ["typst", "book.md", "--no-toc"]).exit_code == 0
    with pymupdf.open(tmp_path / "book.pdf") as doc:
        page = next(p for p in doc if "Первый абзац" in p.get_text())
        x = {line["spans"][0]["text"].split()[0]: line["bbox"][0]
             for block in page.get_text("dict")["blocks"] for line in block.get("lines", [])}
    assert x["Первый"] == x["Третий"]                       # after a chapter or section heading: flush
    assert x["Второй"] > x["Первый"] and x["Четвёртый"] > x["Третий"]  # following paragraphs: indented


@needs_tools
def test_relative_template_path_with_output_elsewhere(tmp_path, monkeypatch):
    (tmp_path / "typesetting").mkdir()
    (tmp_path / "pdf").mkdir()
    (tmp_path / "typesetting" / "my.typ").write_text(typst_processor.TEMPLATE_PATH.read_text())
    (tmp_path / "book.md").write_text('---\ntitle: "Книга"\nauthor: "Автор"\n---\n\n# Глава\n\nТекст.\n')
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(main, ["typst", "book.md", "--template", "typesetting/my.typ", "-o", "pdf/book.pdf"])
    assert result.exit_code == 0, result.output
    assert (tmp_path / "pdf" / "book.pdf").is_file()
