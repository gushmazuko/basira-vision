"""basira capture: pages photographed with a phone are read into the same page file ocr writes."""

import io
import json
import os
import shutil
import signal
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from click.testing import CliRunner
from PIL import Image, TiffImagePlugin, TiffTags

from basira_vision import cli
from basira_vision.capture import CaptureError, CaptureServer, CaptureSession
from basira_vision.capture import server as capture_server
from basira_vision.cli import main
from basira_vision.pipeline import MAX_CONSECUTIVE_FAILURES
from basira_vision.ui.events import PageFailed, PageRead, PageStarted, PhotoReceived, ReadingStopped
from basira_vision.utils.constants import SeparatorType
from basira_vision.utils.errors import AuthenticationError, UnfinishedReplyError, VisionProviderError
from basira_vision.utils.pages import PageFile, read_page_document
from conftest import FakeProvider

KEY = "testkey1"
WAIT = 10  # seconds a test waits for the worker before it fails


def photo(size=(40, 60), orientation=None, fmt="JPEG") -> bytes:
    image = Image.new("RGB", size, "white")
    buffer = io.BytesIO()
    options = {}
    if orientation:
        exif = Image.Exif()
        exif[0x0112] = orientation
        options["exif"] = exif
    image.save(buffer, format=fmt, **options)
    return buffer.getvalue()


HEIC = b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00mif1heic" + b"\x00" * 64


def dng() -> bytes:
    """A file shaped like a RAW photo (DNG): a TIFF that starts with a small preview of the picture."""
    directory = TiffImagePlugin.ImageFileDirectory_v2()
    directory[50706] = bytes([1, 4, 0, 0])  # DNGVersion
    directory.tagtype[50706] = TiffTags.BYTE
    buffer = io.BytesIO()
    Image.new("RGB", (256, 192), "white").save(buffer, format="TIFF", tiffinfo=directory)
    return buffer.getvalue()


NOT_A_PHOTO = "this file is not a JPEG or PNG photo"  # the one answer to everything else


@pytest.fixture
def book(tmp_path):
    """Open (or reopen) a capture session on tmp_path/book; every session is closed afterwards."""
    opened = []

    def _open(provider=None, start=True):
        provider = provider or FakeProvider()
        page_file = PageFile(tmp_path / "book" / "book.md", SeparatorType.OCR, {"title": ""}, resume=True)
        events = []
        session = CaptureSession(page_file, tmp_path / "book" / "images",
                                 lambda path: provider.extract_text(path, "PROMPT"), events.append)
        session.provider, session.events = provider, events
        provider.on_retry = session.on_retry
        opened.append(session)
        if start:
            session.start()
        return session

    yield _open
    for session in opened:
        for gate in getattr(session.provider, "gates", []):
            gate.set()
        session.close()


def shoot(session, page, data=None, replace=False):
    """Hand the session an uploaded file, like the HTTP layer does."""
    upload = session.images_dir.parent / f"upload_{page}_{time.monotonic_ns()}.bin"
    upload.parent.mkdir(parents=True, exist_ok=True)
    upload.write_bytes(data if data is not None else photo())
    try:
        return session.add(page, upload, replace=replace)
    finally:
        upload.unlink()


def idle(session):
    assert session.wait_idle(WAIT), "the worker did not finish in time"


def states(session):
    return {p["page"]: p["state"] for p in session.snapshot()["pages"]}


def file_pages(session):
    _, pages = read_page_document(session.page_file.path.read_text(encoding="utf-8"))
    return {p.number: (p.text, p.error) for p in pages}


def hold(provider, page):
    """Make the provider wait on this page until the returned gate is set; `reached` tells when it got there."""
    gate, reached = threading.Event(), threading.Event()
    provider.gates = getattr(provider, "gates", []) + [gate]

    def wait():
        reached.set()
        assert gate.wait(WAIT)

    provider.on_page[page] = wait
    return gate, reached


# ---------------------------------------------------------------------------- session


def test_pages_are_numbered_from_one_and_read_into_the_page_file(book):
    session = book()
    assert session.snapshot()["next_page"] == 1

    shoot(session, 1)
    assert session.snapshot()["next_page"] == 2
    shoot(session, 2)
    idle(session)

    assert file_pages(session) == {1: ("text of page 1", None), 2: ("text of page 2", None)}
    assert sorted(p.name for p in session.images_dir.iterdir()) == ["page_001.jpg", "page_002.jpg"]
    snapshot = session.snapshot()
    assert snapshot["name"] == "book" and snapshot["next_page"] == 3 and snapshot["stopped"] is None
    assert [(p["page"], p["state"], p["preview"]) for p in snapshot["pages"]] == [
        (1, "done", "text of page 1"), (2, "done", "text of page 2")]
    assert session.provider.ocr_calls == [1, 2]
    received = [e for e in session.events if isinstance(e, PhotoReceived)]
    assert [(e.page, e.retake) for e in received] == [(1, False), (2, False)]
    reading = [e for e in session.events if not isinstance(e, PhotoReceived)]
    assert [type(e) for e in reading] == [PageStarted, PageRead, PageStarted, PageRead]
    assert reading[1].preview == "text of page 1"
    # Whichever thread reports, a page's events come in the order things happened to it.
    for page in (1, 2):
        own = [type(e) for e in session.events if e.page == page]
        assert own == [PhotoReceived, PageStarted, PageRead]


def test_a_book_can_start_from_any_page_number(book):
    session = book()
    shoot(session, 10)
    assert session.snapshot()["next_page"] == 11
    shoot(session, 11)
    idle(session)
    assert sorted(file_pages(session)) == [10, 11]


def test_a_skipped_page_is_inserted_and_numbering_goes_on_after_the_last_page(book):
    session = book()
    for page in (1, 2, 3, 5, 6):
        shoot(session, page)
    shoot(session, 4)
    assert session.snapshot()["next_page"] == 7
    idle(session)
    assert list(file_pages(session)) == [1, 2, 3, 4, 5, 6]  # in page order, whatever the shooting order


def test_a_page_that_is_already_there_is_not_replaced_without_asking(book):
    session = book()
    shoot(session, 1)
    idle(session)
    with pytest.raises(CaptureError) as error:
        shoot(session, 1)
    assert error.value.status == 409 and "page 1 is already there" in str(error.value)
    idle(session)
    assert session.provider.ocr_calls == [1]


def test_retake_replaces_the_page_and_its_photo(book):
    session = book()
    shoot(session, 1, photo(size=(40, 60)))
    shoot(session, 2)
    idle(session)
    first_version = session.snapshot()["pages"][0]["version"]

    gate, reached = hold(session.provider, 2)
    shoot(session, 2, replace=True)  # keeps the worker busy
    assert reached.wait(WAIT)
    session.provider.on_page[1] = lambda: None
    shoot(session, 1, photo(size=(80, 20)), replace=True)
    # Waiting for its turn, the page no longer shows the text of the old photo.
    assert states(session)[1] == "queued" and 1 not in file_pages(session)
    gate.set()
    idle(session)

    assert file_pages(session)[1] == ("text of page 1", None)
    assert session.provider.ocr_calls == [1, 2, 2, 1]
    with Image.open(session.images_dir / "page_001.jpg") as image:
        assert image.size == (80, 20)
    assert session.snapshot()["pages"][0]["version"] > first_version
    assert session.snapshot()["next_page"] == 3  # a retake does not move the numbering


def test_shooting_does_not_wait_for_reading_pages_queue_up(book):
    session = book()
    gate, reached = hold(session.provider, 1)
    shoot(session, 1)
    assert reached.wait(WAIT)
    shoot(session, 2)
    shoot(session, 3)
    assert states(session) == {1: "reading", 2: "queued", 3: "queued"}
    assert not session.wait_idle(0.05)
    gate.set()
    idle(session)
    assert states(session) == {1: "done", 2: "done", 3: "done"}
    assert session.provider.ocr_calls == [1, 2, 3]


def test_a_failed_page_shows_the_reason_and_a_retake_fixes_it(book):
    session = book()
    session.provider.fail_pages = {2}
    for page in (1, 2, 3):
        shoot(session, page)
    idle(session)

    [failed] = [p for p in session.snapshot()["pages"] if p["state"] == "failed"]
    assert (failed["page"], failed["reason"]) == (2, "boom on page 2")
    assert "<!-- Page 2 - Error: boom on page 2 -->" in session.page_file.path.read_text()
    assert states(session) == {1: "done", 2: "failed", 3: "done"}
    assert any(isinstance(e, PageFailed) and e.page == 2 for e in session.events)

    session.provider.fail_pages = set()
    shoot(session, 2, replace=True)
    idle(session)
    assert file_pages(session)[2] == ("text of page 2", None)
    assert states(session)[2] == "done"


def test_an_unfinished_reply_is_the_pages_failure_reason(book):
    session = book()
    session.provider.raise_on_page = {1: UnfinishedReplyError("reply stopped: RECITATION")}
    shoot(session, 1)
    idle(session)
    [page] = session.snapshot()["pages"]
    assert (page["state"], page["reason"]) == ("failed", "reply stopped: RECITATION")


def test_a_rejected_key_stops_reading_and_keeps_the_photos_for_the_next_run(book):
    provider = FakeProvider()
    provider.raise_on_page = {2: AuthenticationError("the endpoint rejected the API key (HTTP 403)")}
    session = book(provider)
    for page in (1, 2, 3):
        shoot(session, page)
    idle(session)

    snapshot = session.snapshot()
    assert snapshot["stopped"] == "the endpoint rejected the API key (HTTP 403)"
    assert states(session) == {1: "done", 2: "queued", 3: "queued"}
    assert sorted(file_pages(session)) == [1]  # the page in flight stays missing, like in ocr
    assert [e.reason for e in session.events if isinstance(e, ReadingStopped)] == [snapshot["stopped"]]

    shoot(session, 4)  # still accepted, nothing is lost
    idle(session)
    assert states(session)[4] == "queued" and provider.ocr_calls == [1, 2]
    session.close()

    # The same command again: the photos that were not read are read now.
    again = book()
    idle(again)
    assert again.provider.ocr_calls == [2, 3, 4]
    assert states(again) == {n: "done" for n in (1, 2, 3, 4)}
    assert again.snapshot()["next_page"] == 5


def test_reading_stops_after_failures_in_a_row_because_the_endpoint_is_down(book):
    provider = FakeProvider()
    count = MAX_CONSECUTIVE_FAILURES
    provider.raise_on_page = {1: UnfinishedReplyError("empty reply")}  # that page's own problem: not counted
    provider.fail_pages = set(range(2, count + 3))
    gate, reached = hold(provider, 1)
    session = book(provider)
    for page in range(1, count + 3):
        shoot(session, page)
    gate.set()
    idle(session)

    snapshot = session.snapshot()
    assert snapshot["stopped"] == f"{count} pages in a row failed: boom on page {count + 1}"
    assert provider.ocr_calls == list(range(1, count + 2))
    assert states(session)[count + 2] == "queued"


def test_the_photo_is_turned_upright_when_it_is_saved(book):
    session = book()
    shoot(session, 1, photo(size=(40, 60), orientation=6))  # the phone was held sideways
    idle(session)
    with Image.open(session.images_dir / "page_001.jpg") as image:
        assert image.size == (60, 40)
        assert image.getexif().get(0x0112) in (None, 1)


@pytest.mark.parametrize("data", [
    pytest.param(HEIC, id="heic"),
    pytest.param(dng(), id="raw (dng): Pillow would read its small preview as the page"),
    pytest.param(photo(fmt="TIFF"), id="tiff"),
    pytest.param(photo(fmt="WEBP"), id="webp"),
    pytest.param(photo(fmt="GIF"), id="gif"),
    pytest.param(photo(fmt="BMP"), id="bmp"),
    pytest.param(photo()[:200], id="a jpeg that was cut off"),
    pytest.param(b"this is not a photo", id="text"),
    pytest.param(b"", id="empty"),
])
def test_only_jpeg_and_png_are_taken_and_everything_else_gets_one_answer(book, data):
    session = book()
    with pytest.raises(CaptureError) as error:
        shoot(session, 1, data)
    assert error.value.status == 415 and str(error.value) == NOT_A_PHOTO
    idle(session)
    assert session.snapshot()["pages"] == [] and session.snapshot()["next_page"] == 1
    assert not session.images_dir.exists() or list(session.images_dir.iterdir()) == []
    assert session.provider.ocr_calls == []


def test_a_jpeg_with_a_second_picture_inside_is_a_jpeg(book):
    # Phones write these (a depth map, a second exposure); Pillow calls the format MPO.
    buffer = io.BytesIO()
    Image.new("RGB", (40, 60), "white").save(buffer, format="MPO", save_all=True,
                                             append_images=[Image.new("RGB", (40, 60), "black")])
    session = book()
    shoot(session, 1, buffer.getvalue())
    idle(session)
    assert states(session) == {1: "done"}
    with Image.open(session.images_dir / "page_001.jpg") as image:
        assert image.format == "JPEG" and image.size == (40, 60) and min(image.getpixel((20, 30))) > 240


def test_a_png_photo_is_accepted_and_saved_as_jpeg(book):
    session = book()
    shoot(session, 1, photo(fmt="PNG"))
    idle(session)
    with Image.open(session.images_dir / "page_001.jpg") as image:
        assert image.format == "JPEG"


@pytest.mark.parametrize("page", [0, -3, 100000])
def test_page_numbers_outside_the_book_are_refused(book, page):
    session = book()
    with pytest.raises(CaptureError) as error:
        shoot(session, page)
    assert error.value.status == 400


def test_running_again_continues_the_book(book):
    first = book()
    first.provider.fail_pages = {2}
    for page in (1, 2, 3):
        shoot(first, page)
    idle(first)
    first.close()

    again = book()
    idle(again)
    # Like --resume: the failed page is read again from its photo, finished pages are not.
    assert again.provider.ocr_calls == [2]
    snapshot = again.snapshot()
    assert [(p["page"], p["state"], p["preview"]) for p in snapshot["pages"]] == [
        (1, "done", "text of page 1"), (2, "done", "text of page 2"), (3, "done", "text of page 3")]
    assert snapshot["next_page"] == 4
    assert all(p["image"] for p in snapshot["pages"])


def test_a_retake_during_reading_discards_the_reading_of_the_old_photo(book):
    session = book()
    gate, reached = hold(session.provider, 1)
    shoot(session, 1)
    assert reached.wait(WAIT)
    session.provider.extract_text = lambda path, prompt: "new photo"  # what the second reading returns
    shoot(session, 1, replace=True)
    gate.set()  # the first reading ends now, with "text of page 1" for a photo that is gone
    idle(session)
    assert file_pages(session) == {1: ("new photo", None)}
    assert [type(e).__name__ for e in session.events].count("PageRead") == 1
    assert [e.retake for e in session.events if isinstance(e, PhotoReceived)] == [False, True]


def test_the_preview_is_the_first_lines_of_the_page(book):
    session = book()
    long_text = "Title\n\nFirst paragraph of the page.\nSecond line.\nThird line.\n" + "x" * 2000
    session.provider.extract_text = lambda path, prompt: long_text
    shoot(session, 1)
    idle(session)
    preview = session.snapshot()["pages"][0]["preview"]
    assert preview.startswith("Title\nFirst paragraph of the page.\nSecond line.")
    assert len(preview) <= 240 and "x" * 300 not in preview


def test_thumbnails_are_small_jpegs_made_on_the_server(book):
    session = book()
    shoot(session, 1, photo(size=(1200, 1600)))
    idle(session)
    data = session.thumbnail(1)
    with Image.open(io.BytesIO(data)) as image:
        assert image.format == "JPEG" and max(image.size) <= 320
    assert session.thumbnail(2) is None


def test_retries_are_reported_and_counted(book):
    session = book()
    session.provider.retry_pages = {1}
    shoot(session, 1)
    idle(session)
    assert session.retries == 1
    assert [type(e).__name__ for e in session.events] == ["PhotoReceived", "PageStarted", "PageRetrying", "PageRead"]


def test_close_lets_the_current_page_finish_and_leaves_the_rest_for_the_next_run(book):
    session = book()
    gate, reached = hold(session.provider, 1)
    shoot(session, 1)
    shoot(session, 2)
    assert reached.wait(WAIT)
    closer = threading.Thread(target=session.close)
    closer.start()
    gate.set()
    closer.join(WAIT)
    assert not closer.is_alive()
    assert session.provider.ocr_calls == [1]
    assert states(session) == {1: "done", 2: "queued"}
    assert session.unread() == [2]


def test_no_photo_is_taken_once_the_session_is_ending(book):
    session = book()
    gate, reached = hold(session.provider, 1)
    shoot(session, 1)
    assert reached.wait(WAIT)
    closer = threading.Thread(target=session.close)
    closer.start()
    wait_for(lambda: session._closing, "the session to start closing")
    # Saved now, it would be left unread without a word: the phone is told instead.
    with pytest.raises(CaptureError) as error:
        shoot(session, 2)
    assert error.value.status == 503 and "the session is ending" in str(error.value)
    gate.set()
    closer.join(WAIT)
    assert states(session) == {1: "done"}
    assert [p.name for p in session.images_dir.iterdir()] == ["page_001.jpg"]


def test_abort_gives_up_the_page_being_read_and_writes_nothing_more(book):
    session = book()
    gate, reached = hold(session.provider, 1)
    shoot(session, 1)
    assert reached.wait(WAIT)
    session.abort()
    assert session.finish() == "Incomplete"
    seen = len(session.events)
    gate.set()  # the request ends after the command has summed up
    session._worker.join(WAIT)
    assert not session._worker.is_alive()
    assert file_pages(session) == {} and session.unread() == [1]
    assert session.events[seen:] == []


def test_reading_stops_when_the_book_cannot_be_written_and_the_photos_wait(book):
    session = book()
    written = session.page_file.write_page

    def full_disk(number, text):
        raise OSError(28, "No space left on device")

    session.page_file.write_page = full_disk
    shoot(session, 1)
    idle(session)  # the worker did not die with the page left "reading" forever
    assert session.stopped == "cannot write book.md: No space left on device"
    assert states(session) == {1: "queued"} and session.unread() == [1]
    assert [e.reason for e in session.events if isinstance(e, ReadingStopped)] == [session.stopped]
    shoot(session, 2)
    assert states(session) == {1: "queued", 2: "queued"}
    session.close()

    session.page_file.write_page = written
    again = book()
    idle(again)
    assert again.provider.ocr_calls == [1, 2] and states(again) == {1: "done", 2: "done"}


def test_nothing_is_reported_after_the_session_has_summed_up(book):
    session = book()
    shoot(session, 1)
    idle(session)
    session.close()
    session.finish()
    seen = len(session.events)
    session.on_retry("HTTP 503", 1, 2, 0.0, False)
    with pytest.raises(CaptureError):
        shoot(session, 2)
    assert session.events[seen:] == []


def test_a_16_bit_picture_keeps_its_greys(book):
    session = book()
    buffer = io.BytesIO()
    Image.new("I;16", (40, 60), 32768).save(buffer, format="PNG")  # mid grey
    shoot(session, 1, buffer.getvalue())
    idle(session)
    with Image.open(session.images_dir / "page_001.jpg") as image:
        low, high = image.convert("L").getextrema()
    assert 120 <= low and high <= 136  # not cut off to white


def test_a_picture_too_large_for_a_jpeg_is_refused_with_a_message(book):
    session = book()
    buffer = io.BytesIO()
    Image.new("L", (70000, 10), 255).save(buffer, format="PNG")
    with pytest.raises(CaptureError) as error:
        shoot(session, 1, buffer.getvalue())
    assert error.value.status == 415 and "70000 x 10 pixels" in str(error.value)
    assert not session.images_dir.exists() or list(session.images_dir.iterdir()) == []


def test_the_photo_taken_here_wins_over_other_files_for_the_same_page(book, tmp_path):
    images = tmp_path / "book" / "images"
    images.mkdir(parents=True)
    (images / "page_0001.jpg").write_bytes(photo(size=(10, 10)))
    (images / "page_001.png").write_bytes(photo(size=(20, 20), fmt="PNG"))
    first = book()
    idle(first)
    shoot(first, 1, photo(size=(80, 20)), replace=True)  # writes page_001.jpg
    idle(first)
    first.close()

    again = book()
    with Image.open(io.BytesIO(again.view(1))) as image:
        assert image.size == (80, 20)


def test_a_report_that_cannot_be_made_does_not_stop_the_reading(book, tmp_path):
    provider = FakeProvider()
    page_file = PageFile(tmp_path / "book" / "book.md", SeparatorType.OCR, {"title": ""}, resume=True)

    def broken_terminal(event):
        raise BrokenPipeError("the terminal is gone")

    session = CaptureSession(page_file, tmp_path / "book" / "images",
                             lambda path: provider.extract_text(path, "PROMPT"), broken_terminal)
    session.start()
    try:
        shoot(session, 1)
        shoot(session, 2)
        idle(session)
        assert states(session) == {1: "done", 2: "done"}
    finally:
        session.close()


def test_a_picture_with_more_pixels_than_any_phone_takes_is_refused_by_name(book):
    session = book()
    buffer = io.BytesIO()
    Image.new("1", (15000, 15000), 1).save(buffer, format="PNG")  # 225 megapixels in a few kilobytes
    with pytest.raises(CaptureError) as error:
        shoot(session, 1, buffer.getvalue())
    assert error.value.status == 415 and "too large (more than 210 megapixels)" in str(error.value)


def test_a_transparent_picture_is_put_on_white_not_on_black(book):
    session = book()
    buffer = io.BytesIO()
    Image.new("RGBA", (40, 60), (0, 0, 0, 0)).save(buffer, format="PNG")
    shoot(session, 1, buffer.getvalue())
    idle(session)
    with Image.open(session.images_dir / "page_001.jpg") as image:
        assert image.convert("L").getextrema()[0] > 240


def test_files_in_the_images_folder_that_are_not_pages_are_left_alone(book, tmp_path):
    images = tmp_path / "book" / "images"
    images.mkdir(parents=True)
    for name in ("page_000.jpg", "page_100000.jpg", "cover.jpg", "page_002.txt", ".page_003.abc.tmp"):
        (images / name).write_bytes(photo())
    session = book()
    idle(session)
    assert session.snapshot()["pages"] == [] and session.provider.ocr_calls == []
    assert not (images / ".page_003.abc.tmp").exists()  # a leftover of a killed session
    assert (images / "cover.jpg").exists() and (images / "page_000.jpg").exists()


def test_a_page_can_be_opened_with_its_whole_text_and_a_large_photo(book):
    session = book()
    long_text = "\n".join(f"line {n}" for n in range(1, 40))
    session.provider.extract_text = lambda path, prompt: long_text
    session.provider.fail_pages = {2}
    shoot(session, 1, photo(size=(3000, 4000)))
    idle(session)
    assert session.page(1) == {"page": 1, "state": "done", "reason": "", "version": 1, "image": True,
                               "text": long_text}
    with Image.open(io.BytesIO(session.view(1))) as image:
        assert image.format == "JPEG" and 1200 < max(image.size) <= 2000
    assert session.page(7) is None and session.view(7) is None

    session.provider.extract_text = FakeProvider.extract_text.__get__(session.provider)
    shoot(session, 2)
    idle(session)
    assert session.page(2)["state"] == "failed" and session.page(2)["text"] == ""
    assert session.page(2)["reason"] == "boom on page 2"


def test_the_last_page_number_is_the_end_of_the_numbering(book):
    session = book()
    shoot(session, 99999)
    assert session.snapshot()["next_page"] == 99999


# ---------------------------------------------------------------------------- http


@pytest.fixture
def served(book):
    session = book()
    server = CaptureServer(session, "127.0.0.1", 0, KEY)
    server.start()
    yield session, f"http://127.0.0.1:{server.port}"
    server.close()


def request(url, data=None, headers=None):
    """(status, content type, body) of a request; HTTP errors are answers, not exceptions."""
    req = urllib.request.Request(url, data=data, headers=headers or {}, method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=WAIT) as response:
            return response.status, response.headers.get("Content-Type", ""), response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.headers.get("Content-Type", ""), error.read()


def wait_for(check, what="condition"):
    deadline = time.monotonic() + WAIT
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}")


def test_the_page_is_served_only_under_the_key(served):
    _, base = served
    status, content_type, body = request(f"{base}/{KEY}/")
    assert status == 200 and content_type.startswith("text/html")
    assert b'capture="environment"' in body and b'accept="image/*"' in body

    for path in ("/", "/wrongkey/", "/wrongkey/status", f"/{KEY}x/", f"/{KEY}/nothing", f"/{KEY}/thumb/1",
                 f"/{KEY}/photo/1", f"/{KEY}/page/1", f"/{KEY}/thumb/%C2%B2", f"/{KEY}/page/x"):
        assert request(base + path)[0] == 404, path
    assert request(f"{base}/wrongkey/upload?page=1", photo())[0] == 404
    assert request(f"{base}/{KEY}/status", photo())[0] == 404  # POST to something that is not the upload


def test_every_other_method_is_answered_like_a_wrong_address(served):
    _, base = served

    def status_of(method, path, data=None):
        req = urllib.request.Request(base + path, data=data, method=method)
        try:
            with urllib.request.urlopen(req, timeout=WAIT) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.read()

    for method in ("PUT", "DELETE", "OPTIONS", "PATCH", "PROPFIND"):
        for path in ("/", f"/{KEY}/", f"/{KEY}/upload?page=1"):
            assert status_of(method, path, b"x" if method in ("PUT", "PATCH") else None)[0] == 404, (method, path)
    assert status_of("HEAD", "/")[0] == 404
    assert status_of("HEAD", f"/{KEY}/") == (200, b"")


def test_a_photo_is_uploaded_read_and_shown_in_the_status(served):
    session, base = served
    status, content_type, body = request(f"{base}/{KEY}/upload?page=1", photo(size=(600, 800)))
    assert status == 200 and json.loads(body) == {"page": 1}

    def done():
        _, _, raw = request(f"{base}/{KEY}/status")
        snapshot = json.loads(raw)
        return snapshot if snapshot["pages"] and snapshot["pages"][0]["state"] == "done" else None

    snapshot = wait_for(done, "page 1 to be read")
    assert snapshot["next_page"] == 2 and snapshot["pages"][0]["preview"] == "text of page 1"
    assert file_pages(session) == {1: ("text of page 1", None)}

    status, content_type, body = request(f"{base}/{KEY}/thumb/1?v=1")
    assert status == 200 and content_type == "image/jpeg"
    with Image.open(io.BytesIO(body)) as image:
        assert max(image.size) <= 320
    status, content_type, body = request(f"{base}/{KEY}/photo/1?v=1")
    assert status == 200 and content_type == "image/jpeg"
    with Image.open(io.BytesIO(body)) as image:
        assert image.size == (600, 800)  # not enlarged
    status, content_type, body = request(f"{base}/{KEY}/page/1")
    assert status == 200 and json.loads(body)["text"] == "text of page 1"


def test_upload_errors_come_back_as_messages_for_the_phone(served, monkeypatch):
    session, base = served
    upload = f"{base}/{KEY}/upload"

    for data in (HEIC, dng()):
        status, _, body = request(f"{upload}?page=1", data)
        assert status == 415 and json.loads(body)["error"] == NOT_A_PHOTO
    # "²" and "١" are digits for str.isdigit, not page numbers
    for query in ("", "?page=", "?page=abc", "?page=0", "?page=%C2%B2", "?page=%D9%A1", "?page=-1", "?page=1.5"):
        status, _, body = request(upload + query, photo())
        assert status == 400 and json.loads(body)["error"], query

    assert request(f"{upload}?page=1", photo())[0] == 200
    status, _, body = request(f"{upload}?page=1", photo())
    assert status == 409 and "page 1 is already there" in json.loads(body)["error"]
    assert request(f"{upload}?page=1&replace=1", photo())[0] == 200

    monkeypatch.setattr(capture_server, "MAX_UPLOAD_BYTES", 100)
    status, _, body = request(f"{upload}?page=2", photo())
    assert status == 413 and "too large" in json.loads(body)["error"]
    # however large the announced size: "too large", not "no length"
    status, _, body = request(f"{upload}?page=2", photo(), {"Content-Length": "9999999999999"})
    assert status == 413
    idle(session)
    assert sorted(p.name for p in session.images_dir.iterdir()) == ["page_001.jpg"]  # no leftovers of refused uploads


def test_an_upload_without_a_length_or_cut_short_is_refused(served):
    session, base = served
    port = int(base.rsplit(":", 1)[1])

    def raw(payload: bytes) -> bytes:
        with socket.create_connection(("127.0.0.1", port), timeout=WAIT) as conn:
            conn.sendall(payload)
            conn.shutdown(socket.SHUT_WR)
            chunks = []
            while chunk := conn.recv(4096):
                chunks.append(chunk)
            return b"".join(chunks)

    # Requests that are not even well formed get the plain answer of a wrong address.
    for line in ("GET http://[/ HTTP/1.1", f"GET /{KEY}/ HTTP/9.9", "what is this", f"GET /{KEY}/" + "x" * 70000):
        assert b" 404 " in raw(f"{line}\r\nHost: x\r\n\r\n".encode()).split(b"\r\n")[0], line[:40]

    head = f"POST /{KEY}/upload?page=1 HTTP/1.1\r\nHost: x\r\n"
    assert b" 411 " in raw(f"{head}\r\n".encode()).split(b"\r\n")[0]
    assert b" 411 " in raw(f"{head}Content-Length: \xb2\r\n\r\n".encode("latin-1")).split(b"\r\n")[0]
    answer = raw(f"{head}Content-Length: 5000\r\n\r\n".encode() + b"only a part")
    assert b" 400 " in answer.split(b"\r\n")[0]
    idle(session)
    assert session.snapshot()["pages"] == []
    assert not session.images_dir.exists() or list(session.images_dir.iterdir()) == []


# ---------------------------------------------------------------------------- command


def run(*args):
    return CliRunner().invoke(main, [str(a) for a in args], catch_exceptions=False)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def capture(*args, phone=None, after_ctrl_c=None):
    """Run `basira capture` and play the phone from another thread; Ctrl-C ends the session.

    ``phone(base)`` gets the address of the page; when it returns, Ctrl-C is sent, and then
    ``after_ctrl_c(ended)`` runs (``ended`` is set once the command has returned). The result
    carries the address as ``result.base``.
    """
    port = free_port()
    base = f"http://127.0.0.1:{port}/{KEY}"
    problems = []
    ended = threading.Event()  # the command returned: a Ctrl-C now would hit the test run itself

    def play():
        try:
            wait_for(lambda: ended.is_set() or _reachable(base), "the capture server")
            if phone and not ended.is_set():
                phone(base)
        except BaseException as e:  # shown by the test instead of hanging it
            problems.append(e)
        finally:
            if not ended.is_set():
                os.kill(os.getpid(), signal.SIGINT)
                if after_ctrl_c and not problems:
                    after_ctrl_c(ended)

    # A Ctrl-C that is sent just as the command returns must not end the test run.
    previous = signal.signal(signal.SIGINT, lambda signum, frame: None)
    thread = threading.Thread(target=play)
    thread.start()
    try:
        result = run("capture", *args, "--host", "127.0.0.1", "--port", port)
    finally:
        ended.set()
        thread.join(WAIT)
        signal.signal(signal.SIGINT, previous)
    if problems:
        raise problems[0]
    result.base = base
    return result


def _reachable(base) -> bool:
    try:
        return request(f"{base}/status")[0] == 200
    except OSError:
        return False


def send(base, page, replace=False):
    status, _, body = request(f"{base}/upload?page={page}" + ("&replace=1" if replace else ""), photo())
    assert status == 200, body


def read_pages(base, *pages):
    def check():
        found = {p["page"]: p["state"] for p in json.loads(request(f"{base}/status")[2])["pages"]}
        return all(found.get(page) in ("done", "failed") for page in pages)

    wait_for(check, f"pages {pages} to be read")


@pytest.fixture
def phone_key(monkeypatch):
    monkeypatch.setattr(cli, "new_key", lambda: KEY)


def book_output(tmp_path):
    return tmp_path / "workspace" / "book" / "book.md"


def shoot_pages(*pages):
    def phone(base):
        for page in pages:
            send(base, page)
        read_pages(base, *pages)

    return phone


def test_capture_writes_the_same_file_ocr_writes(provider, phone_key, tmp_path):
    result = capture("book", "--source-language", "chechen", "--strips", "3", phone=shoot_pages(1, 2))
    assert result.exit_code == 0, result.output

    meta, pages = read_page_document(book_output(tmp_path).read_text())
    assert {p.number: p.text for p in pages} == {1: "text of page 1", 2: "text of page 2"}
    assert meta["source_language"] == "chechen" and meta["strips"] == 3
    assert meta["ai_provider"] == "Gemini" and meta["ai_model"] == "gemini-4-hydrogen"
    assert meta["processing_status"] == "Completed"
    assert "dpi" not in meta and "image_format" not in meta  # they say nothing about a photo
    assert {"title", "author", "basira_version", "custom_prompt", "prompts_dir", "media_resolution",
            "thinking_level", "generated"} <= set(meta)
    assert sorted(p.name for p in (book_output(tmp_path).parent / "images").iterdir()) == [
        "page_001.jpg", "page_002.jpg"]

    [(page, prompt, strips, media_resolution, thinking_level), _] = provider.ocr_requests
    assert "CHECHEN-SPECIFIC RULES" in prompt and (strips, media_resolution, thinking_level) == (3, None, None)

    assert result.stdout == "workspace/book/book.md\n"
    assert f"capture start   input=\"phone camera\" output=workspace/book/book.md url={result.base}/ " in result.stderr
    assert "capture photo   page=1" in result.stderr
    assert "capture ok      page=1" in result.stderr and "capture ok      page=2" in result.stderr
    assert "capture done" in result.stderr and "pages=2/2" in result.stderr
    assert "basira translate workspace/book/book.md --target-language <language>" in result.stderr


def test_the_same_command_again_continues_with_the_books_settings(provider, phone_key, tmp_path):
    assert capture("book", "--source-language", "chechen", "--strips", "3", "--model", "model-a",
                   phone=shoot_pages(1)).exit_code == 0
    provider.ocr_requests.clear()

    result = capture("book", phone=shoot_pages(2))
    assert result.exit_code == 0, result.output
    [(page, prompt, strips, _, _)] = provider.ocr_requests
    assert page == 2 and "CHECHEN-SPECIFIC RULES" in prompt and strips == 3
    assert provider.models[-1] == "model-a"
    assert "1 done earlier" in result.stderr
    meta, pages = read_page_document(book_output(tmp_path).read_text())
    assert [p.number for p in pages] == [1, 2] and meta["source_language"] == "chechen"


def test_capture_stops_when_a_typed_setting_differs_from_the_book(provider, phone_key, tmp_path):
    assert capture("book", "--source-language", "chechen", "--strips", "3", phone=shoot_pages(1)).exit_code == 0
    before = book_output(tmp_path).read_text()

    result = run("capture", "book", "--strips", "0", "--port", "0")
    assert result.exit_code == 1
    assert "was made with other settings (--strips is 0, the file has 3)" in result.stderr
    assert 'text="basira capture book --port 0"' in result.stderr  # the way on has no --resume to type
    assert "--resume" not in result.stderr and "--overwrite" not in result.stderr
    assert book_output(tmp_path).read_text() == before


def test_retake_from_the_phone_replaces_the_page(provider, phone_key, tmp_path):
    def phone(base):
        send(base, 1)
        read_pages(base, 1)
        provider.extract_text = lambda *args, **kwargs: "the retaken page"
        send(base, 1, replace=True)
        wait_for(lambda: "retaken" in json.loads(request(f"{base}/status")[2])["pages"][0]["preview"], "the retake")

    assert capture("book", phone=phone).exit_code == 0
    _, pages = read_page_document(book_output(tmp_path).read_text())
    assert [(p.number, p.text) for p in pages] == [(1, "the retaken page")]


def test_failed_pages_end_the_session_with_exit_code_3(provider, phone_key, tmp_path):
    provider.fail_pages = {2}
    result = capture("book", phone=shoot_pages(1, 2))
    assert result.exit_code == 3
    assert "capture failed  page=2" in result.stderr and "boom on page 2" in result.stderr
    assert read_page_document(book_output(tmp_path).read_text())[0]["processing_status"] == "Incomplete"
    assert "translate" not in result.stderr
    assert "retake" in result.stderr


def test_ctrl_c_finishes_the_current_page_and_the_next_run_reads_the_rest(provider, phone_key, tmp_path):
    release = threading.Event()
    provider.on_page = {1: lambda: release.wait(WAIT)}

    def phone(base):
        send(base, 1)
        send(base, 2)
        wait_for(lambda: json.loads(request(f"{base}/status")[2])["pages"][0]["state"] == "reading", "page 1")

    def let_page_1_finish(ended):  # page 1 is still being read when Ctrl-C arrives
        time.sleep(0.5)
        release.set()

    result = capture("book", phone=phone, after_ctrl_c=let_page_1_finish)
    assert result.exit_code == 130
    assert provider.ocr_calls == [1]
    assert "stopping after the current page" in result.stderr and "capture paused" in result.stderr
    assert 'text="basira capture book --host 127.0.0.1 --port' in result.stderr and "page 2" in result.stderr
    assert result.stdout == ""

    provider.on_page = {}
    result = capture("book", phone=lambda base: read_pages(base, 2))
    assert result.exit_code == 0
    assert provider.ocr_calls == [1, 2]
    assert read_page_document(book_output(tmp_path).read_text())[0]["processing_status"] == "Completed"


def test_a_rejected_key_during_the_session_is_reported_and_ends_with_an_error(provider, phone_key, tmp_path):
    provider.raise_on_page = {1: AuthenticationError("the endpoint rejected the API key (HTTP 403)")}

    def phone(base):
        send(base, 1)
        wait_for(lambda: json.loads(request(f"{base}/status")[2])["stopped"], "reading to stop")

    result = capture("book", phone=phone)
    assert result.exit_code == 1
    assert "capture stopped" in result.stderr and "the endpoint rejected the API key" in result.stderr
    assert 'text="basira capture book --host 127.0.0.1 --port' in result.stderr


def test_ocr_resume_on_a_captured_book_reports_no_conflict(provider, phone_key, make_pdf, tmp_path):
    assert capture("book", "--strips", "3", phone=shoot_pages(1)).exit_code == 0
    result = run("ocr", make_pdf(2), "--resume")
    assert result.exit_code == 0, result.output
    assert provider.ocr_calls == [1, 2]  # only page 2 was read from the PDF
    assert provider.ocr_requests[-1][2] == 3  # with the book's strips


def test_translate_works_on_a_captured_book(provider, phone_key, tmp_path):
    assert capture("book", phone=shoot_pages(1, 2)).exit_code == 0
    result = run("translate", book_output(tmp_path), "--custom-prompt", "CTX[{context_section}] SRC[{text}]")
    assert result.exit_code == 0, result.output
    _, pages = read_page_document((book_output(tmp_path).parent / "book_english.md").read_text())
    assert [p.text for p in pages] == ["tr(text of page 1)", "tr(text of page 2)"]


@pytest.mark.parametrize("name", ["a/b", "..", ".hidden", "a\\b", " ", "a\nb", "a\tb"])
def test_the_book_name_is_a_plain_name(provider, name):
    result = run("capture", name, "--port", "0")
    assert result.exit_code == 2
    assert "Invalid value for 'NAME'" in result.output


def test_a_busy_port_is_an_error_before_anything_is_written(provider, phone_key, tmp_path):
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        result = run("capture", "book", "--host", "127.0.0.1", "--port", busy.getsockname()[1])
    assert result.exit_code == 1
    assert "cannot listen on 127.0.0.1" in result.stderr and "--port" in result.stderr
    assert not book_output(tmp_path).exists()


def test_capture_checks_the_endpoint_before_listening(provider, tmp_path):
    provider.connection_error = AuthenticationError("the endpoint rejected the API key (HTTP 401)")
    result = run("capture", "book", "--port", "0")
    assert result.exit_code == 1 and "rejected the API key" in result.stderr
    assert not book_output(tmp_path).exists()


def test_a_session_without_photos_ends_cleanly(provider, phone_key, tmp_path):
    result = capture("book")
    assert result.exit_code == 0, result.output
    assert "capture done" in result.stderr and "translate" not in result.stderr


def test_provider_errors_other_than_basira_errors_fail_the_page_not_the_worker(book):
    session = book()
    session.provider.raise_on_page = {1: RuntimeError("unexpected")}
    shoot(session, 1)
    shoot(session, 2)
    idle(session)
    assert states(session) == {1: "failed", 2: "done"}
    assert "unexpected" in session.snapshot()["pages"][0]["reason"]
    assert not isinstance(session.provider.raise_on_page[1], VisionProviderError)


def reading_stopped(base):
    wait_for(lambda: json.loads(request(f"{base}/status")[2])["stopped"], "reading to stop")


def test_a_failed_page_that_waits_to_be_read_again_is_counted_once(provider, phone_key, tmp_path):
    provider.fail_pages = {2, 3}
    assert capture("book", phone=shoot_pages(1, 2, 3)).exit_code == 3
    provider.fail_pages = set()
    provider.raise_on_page = {2: AuthenticationError("the endpoint rejected the API key (HTTP 403)")}
    result = capture("book", phone=reading_stopped)
    assert result.exit_code == 1
    assert "capture stopped pages=1/3" in result.stderr  # a book of three pages, not of five


def test_a_typed_prompt_that_is_not_the_books_does_not_come_back_in_the_way_on(provider, phone_key, tmp_path):
    assert capture("book", "--custom-prompt", "PROMPT ONE", phone=shoot_pages(1)).exit_code == 0

    result = run("capture", "book", "--custom-prompt", "PROMPT TWO", "--port", "0")
    assert result.exit_code == 1 and "was made with other settings" in result.stderr
    assert 'text="basira capture book --port 0"' in result.stderr and "PROMPT TWO" not in result.stderr

    # The book's own prompt has to be typed again (its text is not in the file), so it stays in the way on.
    result = run("capture", "book", "--custom-prompt", "PROMPT ONE", "--strips", "2", "--port", "0")
    assert result.exit_code == 1
    assert "basira capture book --port 0 --custom-prompt 'PROMPT ONE'" in result.stderr

    result = run("capture", "book", "--port", "0")
    assert result.exit_code == 1 and "pass the same --custom-prompt text again" in result.stderr
    assert "--resume" not in result.stderr and "--overwrite" not in result.stderr


@pytest.mark.parametrize("content", [
    "Just some notes\n",
    '---\nsource_language: "english"\ntarget_language: "russian"\n---\n\n<!-- Page 1 -->\ntext\n',
])
def test_capture_refuses_a_file_that_is_not_a_book(provider, tmp_path, content):
    path = book_output(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(content)
    result = run("capture", "book", "--port", "0")
    assert result.exit_code == 1
    assert "exists and is not a book read by basira ocr or capture" in result.stderr
    assert path.read_text() == content


def test_a_damaged_header_stops_capture_and_names_a_way_out_that_exists(provider, tmp_path):
    path = book_output(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("---\ntitle: [unclosed\n---\n\n<!-- Page 1 -->\ntext\n")
    result = run("capture", "book", "--port", "0")
    assert result.exit_code == 1 and "has a frontmatter that cannot be read" in result.stderr
    assert "delete workspace/book/book.md and run the command again" in result.stderr
    assert "--overwrite" not in result.stderr


def test_a_second_ctrl_c_does_not_wait_and_nothing_is_written_after_the_summary(provider, phone_key, tmp_path):
    release = threading.Event()
    provider.on_page = {1: lambda: release.wait(WAIT)}

    def phone(base):
        send(base, 1)
        wait_for(lambda: json.loads(request(f"{base}/status")[2])["pages"][0]["state"] == "reading", "page 1")

    def ctrl_c_again(ended):
        while not ended.wait(0.3):  # until it is taken as the second one
            os.kill(os.getpid(), signal.SIGINT)

    try:
        result = capture("book", phone=phone, after_ctrl_c=ctrl_c_again)
        assert result.exit_code == 130 and "capture paused" in result.stderr
        assert 'text="basira capture book --host 127.0.0.1 --port' in result.stderr
        summary = book_output(tmp_path).read_text()
    finally:
        release.set()  # the request ends only now
    for thread in threading.enumerate():
        if thread.name == "basira-capture-reader":
            thread.join(WAIT)
    assert book_output(tmp_path).read_text() == summary
    meta, pages = read_page_document(summary)
    assert pages == [] and meta["processing_status"] == "Incomplete"


def test_ctrl_c_before_the_first_photo_still_ends_the_session_in_order(provider, phone_key, tmp_path, monkeypatch):
    describe = cli._model_details

    def ctrl_c_while_starting(*args):
        os.kill(os.getpid(), signal.SIGINT)
        return describe(*args)

    monkeypatch.setattr(cli, "_model_details", ctrl_c_while_starting)
    result = run("capture", "book", "--host", "127.0.0.1", "--port", "0")
    assert result.exit_code == 0, result.output
    assert "capture done" in result.stderr and "interrupted" not in result.stderr
    assert read_page_document(book_output(tmp_path).read_text())[0]["processing_status"] == "Incomplete"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_logic_of_the_phone_page():
    """Numbering, the upload queue, retakes and the opened page, run against a fake DOM."""
    page = Path(capture_server.__file__).with_name("page.html")
    check = Path(__file__).with_name("capture_page_check.js")
    result = subprocess.run(["node", str(check), str(page)], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr


def test_a_book_name_that_starts_with_a_dash_is_refused(provider):
    result = run("capture", "--port", "0", "--", "-book")
    assert result.exit_code == 2 and "Invalid value for 'NAME'" in result.output


def test_a_failed_page_without_a_photo_is_not_promised_another_reading(provider, phone_key, make_pdf, tmp_path):
    provider.fail_pages = {2}
    assert run("ocr", make_pdf(2), "--no-images").exit_code == 3  # page 2 failed, and no image was kept
    provider.fail_pages = set()

    result = capture("book")
    assert result.exit_code == 3
    assert "capture hint    label=retake" in result.stderr and "page 2 has no photo" in result.stderr
    assert "reads page 2 again" not in result.stderr
    assert provider.ocr_calls == [1, 2]  # nothing was read in the capture session


def test_an_impatient_second_ctrl_c_still_gets_the_summary(provider, phone_key, tmp_path):
    def more_ctrl_c(ended):  # right behind the first one, while the session is closing down
        while not ended.wait(0.02):
            os.kill(os.getpid(), signal.SIGINT)

    result = capture("book", phone=shoot_pages(1, 2), after_ctrl_c=more_ctrl_c)
    assert result.exit_code == 0, result.output
    assert "capture done" in result.stderr and "pages=2/2" in result.stderr
    assert "interrupted" not in result.stderr
    assert read_page_document(book_output(tmp_path).read_text())[0]["processing_status"] == "Completed"


def test_a_book_that_never_got_a_photo_is_started_anew_with_the_options_typed_now(provider, phone_key, tmp_path):
    assert capture("book").exit_code == 0  # the language was forgotten; only the header was written
    result = capture("book", "--source-language", "chechen", "--strips", "3", phone=shoot_pages(1))
    assert result.exit_code == 0, result.output
    meta, pages = read_page_document(book_output(tmp_path).read_text())
    assert meta["source_language"] == "chechen" and meta["strips"] == 3 and len(pages) == 1


def test_a_book_ocr_left_incomplete_is_not_called_complete_by_capture(provider, phone_key, make_pdf, tmp_path):
    assert run("ocr", make_pdf(5), "--page-range", "1-2").exit_code == 0  # pages 3 to 5 are still in the PDF
    result = capture("book")
    assert result.exit_code == 0
    assert read_page_document(book_output(tmp_path).read_text())[0]["processing_status"] == "Incomplete"
    assert "translate" not in result.stderr


def log_lines(path):
    return [line.split(" ", 2)[2] for line in path.read_text(encoding="utf-8").splitlines()]


def test_capture_keeps_a_log_without_the_key_and_without_the_text(provider, phone_key, tmp_path):
    def phone(base):
        send(base, 1)
        status, _, body = request(f"{base}/upload?page=2", HEIC)  # refused with a message
        assert status == 415
        assert request(f"{base}/upload?page=", photo())[0] == 400  # no page number
        send(base, 1, replace=True)
        read_pages(base, 1)
        wait_for(lambda: json.loads(request(f"{base}/status")[2])["pages"][0]["version"] == 2
                 and json.loads(request(f"{base}/status")[2])["pages"][0]["state"] == "done", "the retake")

    result = capture("book", "--verbose", phone=phone)
    assert result.exit_code == 0, result.output
    text = (book_output(tmp_path).with_suffix(".log")).read_text(encoding="utf-8")
    log = log_lines(book_output(tmp_path).with_suffix(".log"))

    port = result.base.split(":")[2].split("/")[0]
    assert log[0].startswith("capture run     basira=") and f"--port {port} --verbose" in log[0]
    start = next(line for line in log if line.startswith("capture start "))
    assert f"url=http://127.0.0.1:{port}/<key>/" in start
    assert KEY not in text  # also not in the requests --verbose lists
    assert "/<key>/upload?page=1" in text
    assert "text of page" not in text
    assert "capture photo   page=1" in log and "capture photo   page=1 retake=yes" in log
    assert any(line.startswith("capture ok      page=1 ") for line in log)
    refused = [line for line in log if "refused" in line or "could not be opened" in line]
    assert f'capture warning msg="page 2: photo refused: {NOT_A_PHOTO}"' in refused
    assert any(line.startswith("capture info    ") and "page 2: the upload could not be opened" in line
               for line in refused)
    assert 'capture info    msg="upload refused (400): the page number is missing"' in refused
    assert any(line.startswith("capture done ") for line in log)
    assert log[-1].startswith("capture hint ")


def test_a_capture_session_that_cannot_start_leaves_no_log(provider, phone_key, tmp_path):
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        assert run("capture", "book", "--host", "127.0.0.1", "--port", busy.getsockname()[1]).exit_code == 1
    assert not (tmp_path / "workspace").exists()


def test_a_second_session_adds_to_the_books_log(provider, phone_key, tmp_path):
    assert capture("book", phone=shoot_pages(1)).exit_code == 0
    assert run("capture", "book", "--strips", "2", "--port", "0").exit_code == 1  # other settings: refused
    assert capture("book", phone=shoot_pages(2)).exit_code == 0
    log = log_lines(book_output(tmp_path).with_suffix(".log"))
    assert [line.split()[1] for line in log if line.split()[1] in ("run", "error", "done")] == [
        "run", "done", "run", "error", "run", "done"]
