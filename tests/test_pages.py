import yaml

from basira_vision.utils.constants import SeparatorType
from basira_vision.utils.pages import PageFile, parse_pages, read_page_document, split_frontmatter


def new_file(tmp_path, separator=SeparatorType.OCR, **frontmatter):
    meta = {"title": "", "author": "", "processing_status": "In progress", **frontmatter}
    return PageFile(tmp_path / "book.md", separator, meta)


def test_pages_are_written_in_page_order(tmp_path):
    page_file = new_file(tmp_path)
    for number in (3, 1, 2):
        page_file.write_page(number, f"text {number}")

    _, pages = read_page_document(page_file.path.read_text())
    assert [(p.number, p.text) for p in pages] == [(1, "text 1"), (2, "text 2"), (3, "text 3")]


def test_resume_round_trip_keeps_pages_and_errors(tmp_path):
    for separator in SeparatorType:
        page_file = new_file(tmp_path, separator)
        page_file.write_page(1, "first\n\n---\n\nwith a rule inside")
        page_file.fail_page(2, "HTTP 500 INTERNAL")
        page_file.write_page(3, "")

        resumed = PageFile(page_file.path, separator, {}, resume=True)
        assert resumed.completed() == {1, 3}
        assert resumed.failed() == {2: "HTTP 500 INTERNAL"}
        assert resumed.get(1).text == "first\n\n---\n\nwith a rule inside"
        assert resumed.render() == page_file.render()


def test_retrying_a_failed_page_replaces_it_in_place(tmp_path):
    page_file = new_file(tmp_path)
    page_file.write_page(1, "one")
    page_file.fail_page(2, "boom")
    page_file.write_page(3, "three")

    page_file.write_page(2, "two")

    content = page_file.path.read_text()
    assert "Error" not in content
    assert content.index("two") < content.index("three")


def test_set_field_only_touches_frontmatter(tmp_path):
    page_file = new_file(tmp_path)
    page_file.write_page(1, "title: this line is book text\nauthor: so is this one")

    page_file.set_field("title", 'A "quoted" title: part 2')
    page_file.set_field("processing_status", "Completed")

    meta, pages = read_page_document(page_file.path.read_text())
    assert meta["title"] == 'A "quoted" title: part 2'
    assert meta["processing_status"] == "Completed"
    assert pages[0].text == "title: this line is book text\nauthor: so is this one"


def test_new_file_frontmatter_is_valid_yaml(tmp_path):
    page_file = new_file(tmp_path, source_language=None, ai_model="gemini-x")
    meta, _, _ = split_frontmatter(page_file.path.read_text())
    assert meta == yaml.safe_load(page_file.render().split("---\n")[1])
    assert meta["title"] == "" and meta["source_language"] is None and meta["ai_model"] == "gemini-x"


def test_text_without_markers_is_a_single_page():
    assert [(p.number, p.text) for p in parse_pages("\nHand-written text\n\n---\n\nmore\n")] == [
        (1, "Hand-written text\n\n---\n\nmore")
    ]
    assert parse_pages("  \n") == []


def test_document_without_frontmatter_keeps_leading_text():
    meta, pages = read_page_document("Intro text\n\n---\n\nnext part\n")
    assert meta == {}
    assert pages[0].text.startswith("Intro text")


def test_failure_reason_lives_in_the_marker_only(tmp_path):
    page_file = new_file(tmp_path)
    page_file.fail_page(1, "multi\nline --> reason")
    content = page_file.path.read_text()
    assert "<!-- Page 1 - Error: multi line —> reason -->" in content
    _, pages = read_page_document(content)
    assert pages[0].text == "" and pages[0].error == "multi line —> reason"


def test_text_under_an_error_marker_is_a_manual_fix(tmp_path):
    _, pages = read_page_document("<!-- Page 4 - Error: HTTP 500 -->\nfixed by hand\n")
    assert (pages[0].text, pages[0].error) == ("fixed by hand", None)
    _, pages = read_page_document("<!-- Page 4 - Error -->\n")
    assert pages[0].error == "unknown error"
