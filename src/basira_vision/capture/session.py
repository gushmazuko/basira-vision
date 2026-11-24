"""A capture session: photos arrive one at a time and one worker reads them into the page file.

HTTP handler threads call ``add``, ``snapshot`` and ``thumbnail``; the worker thread reads the
queued photos one after another. ``PageFile`` is not thread-safe, so it is only touched under
the session lock. Events are reported under the same lock, so they come out in the order in
which the state changed, whichever thread caused the change.

What is still to read is not stored anywhere else: it is every photo in the images folder
whose page is missing from the page file or failed in it. Running the command again therefore
continues where the session stopped, the way ``ocr --resume`` does.
"""

import io
import os
import re
import threading
import time
import uuid
import warnings
from collections import OrderedDict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

from PIL import Image, ImageOps

from ..pipeline import MAX_CONSECUTIVE_FAILURES
from ..ui.events import PageFailed, PageRead, PageRetrying, PageStarted, PhotoReceived, ReadingStopped
from ..utils.errors import AuthenticationError, BasiraError, RateLimitError, UnfinishedReplyError
from ..utils.logging import get_logger
from ..utils.pages import PageFile

logger = get_logger("capture")

MAX_PAGE = 99999
THUMBNAIL_SIZE = 240
VIEW_SIZE = 2000  # the photo as the phone shows it when a page is opened
_VIEWS_KEPT = 4
PREVIEW_LINES = 3
PREVIEW_CHARS = 200
JPEG_QUALITY = 95  # the same as ocr uses for --image-format JPEG

_IMAGE_FORMATS = ("jpg", "png", "tiff")  # as ocr and capture name them, the preferred one first
_IMAGE_RE = re.compile(rf"^page_(\d+)\.({'|'.join(_IMAGE_FORMATS)})$")
_JPEG_MAX_SIDE = 65500  # what the JPEG format can hold
# Phone cameras go up to 200 megapixels. Pillow warns from 89 and refuses from 179: here the
# warning is switched off (it would land in the middle of the terminal view) and the limit is ours.
MAX_PIXELS = 210 * 10**6
# What a photo may be. Pillow is told to try nothing else, and everything else gets one answer.
# Not "whatever Pillow can open": that takes a RAW photo (a TIFF that starts with a small
# preview) and reads the preview as the page.
PHOTO_FORMATS = ("JPEG", "PNG")
NOT_A_PHOTO = "this file is not a JPEG or PNG photo"


class CaptureError(BasiraError):
    """A photo that cannot be accepted. The message is shown on the phone; ``status`` is the HTTP code."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


@dataclass
class Shot:
    """One page of the session, as the phone shows it."""

    page: int
    state: str  # queued | reading | done | failed
    version: int = 1  # grows with every new photo of the page
    preview: str = ""
    reason: str = ""
    image: Optional[Path] = None


def preview_of(text: str) -> str:
    """The first lines of a page, short enough for a list on a phone."""
    lines = [line.strip() for line in text.splitlines() if line.strip()][:PREVIEW_LINES]
    preview = "\n".join(lines)
    return preview if len(preview) <= PREVIEW_CHARS else preview[:PREVIEW_CHARS].rstrip() + "…"


class CaptureSession:
    def __init__(self, page_file: PageFile, images_dir: Path, read: Callable[[Path], str],
                 emit: Optional[Callable[[Any], None]] = None):
        """
        Args:
            page_file: The book; finished and failed pages are written into it
            images_dir: Where the photos are kept as ``page_NNN.jpg``
            read: Returns the text of a page image (the provider call with the run's settings)
            emit: Receives progress events (``ui.events``)
        """
        self.page_file = page_file
        self.images_dir = images_dir
        self.name = page_file.path.stem
        self.retries = 0
        self._read = read
        self._emit = emit or (lambda event: None)
        self._lock = threading.Lock()
        self._changed = threading.Condition(self._lock)
        self._queue: Deque[Tuple[int, int]] = deque()  # (page, version of its photo)
        self._shots: Dict[int, Shot] = {}
        self._thumbnails: Dict[int, Tuple[int, bytes]] = {}
        self._views: OrderedDict[int, Tuple[int, bytes]] = OrderedDict()  # the last few large previews
        self._aborted = False
        self._finished = False  # the command has summed up: nothing is reported any more
        self._last: Optional[int] = None  # the last page shot as a new one; numbering goes on after it
        self._reading: Optional[int] = None
        self._stopped: Optional[str] = None
        self._closing = False
        self._worker: Optional[threading.Thread] = None
        warnings.filterwarnings("ignore", category=Image.DecompressionBombWarning)
        Image.MAX_IMAGE_PIXELS = max(Image.MAX_IMAGE_PIXELS or 0, MAX_PIXELS // 2 + 1)  # Pillow refuses at twice this
        self._load()

    # ------------------------------------------------------------------ state

    def _load(self) -> None:
        """Pick up the book as an earlier session left it."""
        images: Dict[int, Path] = {}
        if self.images_dir.is_dir():
            for leftover in self.images_dir.glob(".page_*.tmp"):  # ours, from a session that was killed
                leftover.unlink(missing_ok=True)
            ranks: Dict[int, Tuple[int, int]] = {}
            for path in sorted(self.images_dir.iterdir()):
                match = _IMAGE_RE.match(path.name)
                if not match or not 1 <= int(match.group(1)) <= MAX_PAGE:
                    continue
                number = int(match.group(1))
                # A photo taken here is page_NNN.jpg, and that name wins: a page rendered by ocr may be
                # there in another format, and a file may spell the number with more zeros.
                rank = (_IMAGE_FORMATS.index(match.group(2)), 0 if path.name == _image_name(number) else 1)
                if number not in ranks or rank < ranks[number]:
                    images[number], ranks[number] = path, rank
        for number, page in self.page_file.pages.items():
            self._shots[number] = Shot(number, "failed" if page.is_error else "done", preview=preview_of(page.text),
                                       reason=page.error or "", image=images.get(number))
        completed = self.page_file.completed()
        for number in sorted(images):
            if number in completed:
                continue
            shot = self._shots.setdefault(number, Shot(number, "queued", image=images[number]))
            shot.state, shot.reason = "queued", ""
            self._queue.append((number, shot.version))

    def _next_page(self) -> int:
        number = (self._last if self._last is not None else max(self._shots, default=0)) + 1
        while number in self._shots:
            number += 1
        return min(number, MAX_PAGE)

    def page(self, number: int) -> Optional[dict]:
        """One page with its whole text, for the phone's page view; None when there is no such page."""
        with self._lock:
            shot = self._shots.get(number)
            if shot is None:
                return None
            written = self.page_file.get(number)
            return {"page": shot.page, "state": shot.state, "reason": shot.reason, "version": shot.version,
                    "image": shot.image is not None,
                    "text": written.text if written is not None and shot.state == "done" else ""}

    def snapshot(self) -> dict:
        """The state of the session for the phone, as JSON-ready data."""
        with self._lock:
            return {
                "name": self.name,
                "pages": [
                    {"page": s.page, "state": s.state, "preview": s.preview, "reason": s.reason,
                     "version": s.version, "image": s.image is not None}
                    for _, s in sorted(self._shots.items())
                ],
                "next_page": self._next_page(),
                "stopped": self._stopped,
            }

    @property
    def stopped(self) -> Optional[str]:
        """Why reading stopped for good in this session (rejected key, rate limit, endpoint down)."""
        with self._lock:
            return self._stopped

    @property
    def reading(self) -> Optional[int]:
        with self._lock:
            return self._reading

    def unread(self) -> List[int]:
        """Pages whose photo is saved but not read yet."""
        with self._lock:
            return sorted(n for n, s in self._shots.items() if s.state in ("queued", "reading"))

    def photos(self) -> List[int]:
        """Pages that have a photo."""
        with self._lock:
            return sorted(n for n, s in self._shots.items() if s.image is not None)

    def finish(self, keep_incomplete: bool = False) -> str:
        """Write the final ``processing_status`` and return it; from here on nothing is reported.

        ``keep_incomplete``: the book was incomplete for a reason this session cannot see.
        """
        with self._lock:
            self._finished = True
            pending = any(s.state in ("queued", "reading") for s in self._shots.values())
            complete = bool(self.page_file.pages) and not self.page_file.failed() and not pending
            complete = complete and not keep_incomplete
            status = "Completed" if complete else "Incomplete"
            self.page_file.set_field("processing_status", status)
            return status

    # ------------------------------------------------------------------ photos

    def add(self, page: int, upload: Path, replace: bool = False) -> None:
        """Take a photo for a page: save it upright as JPEG and queue it for reading.

        Raises:
            CaptureError: The page number is out of range, the page exists and ``replace`` is
                not set, or the file is not a photo that can be opened
        """
        if not 1 <= page <= MAX_PAGE:
            raise CaptureError(400, f"page numbers go from 1 to {MAX_PAGE}")
        with self._lock:
            self._require_free(page, replace)
        converted = self._convert(upload, page)  # slow for a large photo: outside the lock
        with self._lock:
            try:
                self._require_free(page, replace)
                # The old text belongs to the old photo. Without it the page counts as not read,
                # so a session that ends before the new photo is read still reads it next time.
                self.page_file.remove_page(page)
                target = self.images_dir / _image_name(page)
                os.replace(converted, target)
            except BaseException as e:
                converted.unlink(missing_ok=True)
                if isinstance(e, OSError):
                    raise CaptureError(500, f"basira could not save the photo: {e.strerror or e}") from None
                raise
            shot = self._shots.get(page)
            retake = shot is not None
            if shot is None:
                shot = self._shots[page] = Shot(page, "queued", version=0)
                self._last = page
            shot.version += 1
            shot.state, shot.preview, shot.reason, shot.image = "queued", "", "", target
            self._thumbnails.pop(page, None)
            self._views.pop(page, None)
            self._queue.append((page, shot.version))
            self._report(PhotoReceived(page, retake))  # before the worker can start on it
            self._changed.notify_all()

    def _report(self, event: Any) -> None:
        """Report an event. Called with the lock held, so events keep the order of the changes."""
        if self._finished:
            return
        try:
            self._emit(event)
        except Exception:  # a terminal that is gone must not stop the reading or lose a photo
            logger.debug("an event could not be reported", exc_info=True)

    def _require_free(self, page: int, replace: bool) -> None:
        if self._closing:  # a photo saved now would only be found by the next run, without a word
            raise CaptureError(503, "the session is ending; this photo was not saved")
        if page in self._shots and not replace:
            raise CaptureError(409, f"page {page} is already there; use retake to replace it")

    def _convert(self, upload: Path, page: int) -> Path:
        """Save the upload as an upright JPEG next to its final place; return that temporary file."""
        try:
            with Image.open(upload, formats=PHOTO_FORMATS) as opened:
                if opened.width * opened.height > MAX_PIXELS:
                    raise Image.DecompressionBombError
                # Phones store the sensor's orientation in EXIF instead of turning the pixels.
                image = ImageOps.exif_transpose(opened)
                if image.mode.startswith("I"):
                    # 16 bits per pixel: bring the values to 8 bits; a plain convert() would cut
                    # everything above 255 off and leave a white page.
                    image = image.point(lambda value: value * (1 / 256)).convert("L")
                if image.mode in ("RGBA", "LA", "PA") or "transparency" in image.info:
                    # JPEG has no transparency: put the picture on white, as paper is, not on black.
                    image = image.convert("RGBA")
                    white = Image.new("RGBA", image.size, "white")
                    image = Image.alpha_composite(white, image)
                image = image.convert("RGB")
        except Image.DecompressionBombError:
            raise CaptureError(415, f"this picture is too large (more than {MAX_PIXELS // 10**6} megapixels)") from None
        except Exception as e:
            logger.info(f"page {page}: the upload could not be opened: {type(e).__name__}: {e}")
            raise CaptureError(415, NOT_A_PHOTO) from None
        if max(image.size) > _JPEG_MAX_SIDE:
            raise CaptureError(415, f"this picture is too large to keep as a photo "
                                    f"({image.width} x {image.height} pixels)")
        converted = self.images_dir / f".page_{page:03d}.{uuid.uuid4().hex}.tmp"
        try:
            self.images_dir.mkdir(parents=True, exist_ok=True)
            image.save(converted, format="JPEG", quality=JPEG_QUALITY)
        except Exception as e:  # a full disk, a folder that cannot be written
            converted.unlink(missing_ok=True)
            detail = e.strerror if isinstance(e, OSError) and e.strerror else e
            raise CaptureError(500, f"basira could not save the photo: {detail}") from None
        except BaseException:
            converted.unlink(missing_ok=True)
            raise
        return converted

    def thumbnail(self, page: int) -> Optional[bytes]:
        """A small JPEG of the page's photo for the list, or None when there is no photo."""
        return self._scaled(page, THUMBNAIL_SIZE, 80, self._thumbnails)

    def view(self, page: int) -> Optional[bytes]:
        """A JPEG of the page's photo large enough to check its letters, or None when there is no photo."""
        return self._scaled(page, VIEW_SIZE, 85, self._views)

    def _scaled(self, page: int, size: int, quality: int, cache: Dict[int, Tuple[int, bytes]]) -> Optional[bytes]:
        with self._lock:
            shot = self._shots.get(page)
            if shot is None or shot.image is None:
                return None
            cached = cache.get(page)
            if cached and cached[0] == shot.version:
                return cached[1]
            path, version = shot.image, shot.version
        try:
            with Image.open(path) as image:
                image.draft("RGB", (size, size))  # lets a JPEG be decoded at a fraction of its size
                small = image.convert("RGB")
            small.thumbnail((size, size))
            buffer = io.BytesIO()
            small.save(buffer, format="JPEG", quality=quality)
        except Exception as e:
            logger.debug(f"page {page}: no preview: {type(e).__name__}: {e}")
            return None
        data = buffer.getvalue()
        with self._lock:
            cache[page] = (version, data)
            while cache is self._views and len(cache) > _VIEWS_KEPT:
                self._views.popitem(last=False)
        return data

    # ------------------------------------------------------------------ reading

    def start(self) -> None:
        self._worker = threading.Thread(target=self._work, name="basira-capture-reader", daemon=True)
        self._worker.start()

    def close(self) -> None:
        """Let the page being read finish, then stop; photos still queued stay for the next run.

        From the first moment no new photo is taken: it would be left unread without a word.
        """
        with self._lock:
            self._closing = True
            self._changed.notify_all()
        worker = self._worker
        while worker is not None and worker.is_alive():
            worker.join(0.2)  # in slices, so a second Ctrl-C is not held back

    def abort(self) -> None:
        """Stop at once: the page being read is given up, its photo stays for the next run."""
        with self._lock:
            self._closing = self._aborted = True
            self._changed.notify_all()

    def wait_idle(self, timeout: Optional[float] = None) -> bool:
        """Wait until nothing is being read and nothing more will be; False on timeout."""
        with self._lock:
            return self._changed.wait_for(self._idle, timeout)

    def _idle(self) -> bool:
        if self._reading is not None:
            return False
        return not self._queue or self._stopped is not None or self._closing

    def on_retry(self, reason: str, attempt: int, attempts: int, wait: float, rate_limited: bool) -> None:
        """The provider's retry callback: count the retry and report it for the page being read."""
        with self._lock:
            self.retries += 1
            self._report(PageRetrying(self._reading, reason, attempt, attempts, wait, rate_limited))

    def _work(self) -> None:
        failures_in_a_row = 0
        while True:
            with self._lock:
                while not self._queue and not self._closing:
                    self._changed.wait()
                if self._closing:
                    return
                page, version = self._queue.popleft()
                shot = self._shots[page]
                if shot.version != version:  # a newer photo of this page is queued behind
                    self._changed.notify_all()
                    continue
                shot.state, self._reading = "reading", page
                path = shot.image
                self._report(PageStarted(page, "reading"))
                self._changed.notify_all()

            started = time.monotonic()
            text, failure, fatal = "", None, None
            try:
                text = self._read(path)
            except (AuthenticationError, RateLimitError) as e:
                fatal = str(e)  # every following page would fail the same way
            except BasiraError as e:
                failure = e
            except Exception as e:  # must not kill the worker: the phone would wait forever
                failure = e
                logger.debug(f"page {page}: {type(e).__name__}: {e}")
            seconds = time.monotonic() - started

            with self._lock:
                self._reading = None
                if self._aborted:  # the command has already summed up; write and report nothing more
                    shot.state = "queued"
                    self._changed.notify_all()
                    return
                current = shot.version == version  # else: retaken meanwhile, this reading is of the old photo
                try:
                    if fatal is not None:
                        self._stopped = fatal
                    elif not current:
                        pass
                    elif failure is not None:
                        self.page_file.fail_page(page, str(failure) or type(failure).__name__)
                        shot.state, shot.reason = "failed", self.page_file.get(page).error
                        self._report(PageFailed(page, shot.reason, seconds))
                        if isinstance(failure, UnfinishedReplyError):
                            pass  # this page's problem, not a sign the endpoint is down
                        else:
                            failures_in_a_row += 1
                            if failures_in_a_row >= MAX_CONSECUTIVE_FAILURES:
                                self._stopped = f"{failures_in_a_row} pages in a row failed: {shot.reason}"
                    else:
                        self.page_file.write_page(page, text)
                        shot.state, shot.preview, shot.reason = "done", preview_of(text), ""
                        self._report(PageRead(page, seconds, shot.preview))
                        failures_in_a_row = 0
                except Exception as e:  # the book itself cannot be written: a full disk, a folder that is gone
                    detail = e.strerror if isinstance(e, OSError) and e.strerror else (str(e) or type(e).__name__)
                    self._stopped = f"cannot write {self.page_file.path.name}: {detail}"
                if self._stopped is not None:
                    if current and shot.state == "reading":  # not written: its photo waits for the next run
                        shot.state = "queued"
                        self._queue.appendleft((page, version))
                    self._report(ReadingStopped(self._stopped))
                self._changed.notify_all()
                if self._stopped is not None:
                    return


def _image_name(page: int) -> str:
    return f"page_{page:03d}.jpg"
