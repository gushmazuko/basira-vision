"""The HTTP side of ``basira capture``: one page for the phone, photo uploads, a status feed.

Plain HTTP for one local network, standard library only. Every path lives under a random
key, so a device that was not given the address cannot add pages.

    GET  /<key>/                      the page
    GET  /<key>/status                the session as JSON (the page polls it)
    GET  /<key>/page/<page>           one page with its whole text, as JSON
    GET  /<key>/thumb/<page>          a small JPEG of the page's photo
    GET  /<key>/photo/<page>          a large JPEG of it, to check the letters
    POST /<key>/upload?page=N[&replace=1]   the photo as the request body

Anything else, with or without the key, is answered with 404.
"""

import json
import secrets
import shutil
import socket
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlsplit

from ..utils.logging import get_logger
from .session import CaptureError, CaptureSession

logger = get_logger("capture")

MAX_UPLOAD_BYTES = 64 * 1024 * 1024  # a 48-megapixel JPEG is well below this
# A refused upload is still read up to this size before the answer is sent: closing a
# connection with unread data resets it, and the phone would see no answer at all.
_DRAIN_LIMIT = 256 * 1024 * 1024
_CHUNK = 64 * 1024
_KEY_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"  # easy to type on a phone: no 0/o or 1/l/i
_PAGE_HTML = Path(__file__).with_name("page.html")


def new_key(length: int = 8) -> str:
    """A random key for the address of a session."""
    return "".join(secrets.choice(_KEY_ALPHABET) for _ in range(length))


def _number(text: str, digits: int = 12) -> Optional[int]:
    """A plain decimal number; None for anything else (str.isdigit alone also accepts "²" and "١")."""
    return int(text) if text.isascii() and text.isdigit() and len(text) <= digits else None


def local_address() -> Optional[str]:
    """This computer's address on the local network, or None when it cannot be told."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("192.0.2.1", 9))  # sends nothing: a UDP connect only picks the route
            return probe.getsockname()[0]
    except OSError:
        return None


class _Handler(BaseHTTPRequestHandler):
    server_version = "basira"
    sys_version = ""
    timeout = 120  # a connection that goes silent must not hold a thread forever

    def log_message(self, format, *args):  # noqa: A002 (the base class names it)
        # Without the key: it lets a device into the session, and logs are kept.
        line = (format % args).replace(f"/{self.server.key}", "/<key>")
        logger.debug(f"{self.address_string()} {line}")

    # ------------------------------------------------------------------ routing

    def _resource(self):
        """(path below the key, query) of the request, or None when the key is wrong."""
        try:
            parts = urlsplit(self.path)
        except ValueError:  # e.g. "http://[/": not an address at all
            return None
        key, slash, rest = parts.path.lstrip("/").partition("/")
        if not secrets.compare_digest(key.encode(), self.server.key.encode()):
            return None
        if not slash:  # /<key> without the slash: relative links of the page would break
            return "", None
        return rest, parse_qs(parts.query, keep_blank_values=True)

    def __getattr__(self, name: str):
        # The base class answers 501 for a method it has no do_<METHOD> for, key or no key.
        if name.startswith("do_"):
            return self._not_found
        raise AttributeError(name)

    def _not_found(self):
        self.close_connection = True  # a body that may follow is not read
        self._send(404)

    def send_error(self, code, message=None, explain=None):
        # What the base class finds wrong with a request (a bad version, a line that is too long)
        # gets the same plain answer as a wrong address, with nothing about the server in it.
        logger.debug(f"{self.address_string()} refused: {code} {message or ''}")
        if self.request_version == "HTTP/0.9":
            # Older Pythons leave this default in place when they reject a request line, and an
            # HTTP/0.9 answer has no status line at all: the client would get an empty reply.
            self.request_version = "HTTP/1.0"
        self._not_found()

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        resource = self._resource()
        if resource is None:
            return self._send(404)
        rest, query = resource
        if query is None:
            self.send_response(301)
            self.send_header("Location", f"/{self.server.key}/")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return None
        session: CaptureSession = self.server.session
        if rest == "":
            return self._send(200, _PAGE_HTML.read_bytes(), "text/html; charset=utf-8")
        if rest == "status":
            return self._json(200, {**session.snapshot(), "max_bytes": MAX_UPLOAD_BYTES})
        kind, _, number = rest.partition("/")
        number = _number(number)
        if number is not None and kind == "page":
            page = session.page(number)
            if page is not None:
                return self._json(200, page)
        elif number is not None and kind in ("thumb", "photo"):
            data = session.thumbnail(number) if kind == "thumb" else session.view(number)
            if data is not None:
                # The page asks for thumb/N?v=<version>, so a retake gets a new address.
                return self._send(200, data, "image/jpeg", cache="private, max-age=3600")
        return self._send(404)

    def do_POST(self):
        resource = self._resource()
        if resource is None or resource[1] is None or resource[0] != "upload":
            self.close_connection = True
            return self._send(404)
        query = resource[1]
        length = _number(self.headers.get("Content-Length", "").strip(), digits=40)  # any size is "too large"
        if length is None:
            self.close_connection = True
            return self._refuse(411, "the upload has no length")
        if length > MAX_UPLOAD_BYTES:
            if length > _DRAIN_LIMIT or not self._discard(length):
                self.close_connection = True
            megabytes = -(-length // 2**20)  # rounded up: 64 MB and a byte is "65 MB", over a limit of 64
            return self._refuse(413, f"the photo is too large ({megabytes} MB; "
                                     f"the limit is {MAX_UPLOAD_BYTES // 2**20} MB)")

        upload = self._receive(length)
        if upload is None:
            self.close_connection = True
            return self._refuse(400, "the upload was cut short; try again")
        try:
            page = _number((query.get("page") or [""])[0])
            if page is None:
                return self._refuse(400, "the page number is missing")
            replace = (query.get("replace") or ["0"])[0] == "1"
            try:
                self.server.session.add(page, upload, replace=replace)
            except CaptureError as e:
                # While the session is ending this is expected, and the summary may be out already.
                (logger.debug if e.status == 503 else logger.warning)(f"page {page}: photo refused: {e}")
                return self._json(e.status, {"error": str(e)})
            except Exception as e:  # the phone gets an answer, not a connection that just closes
                logger.warning(f"page {page}: photo not saved: {type(e).__name__}: {e}")
                return self._json(500, {"error": f"basira could not save the photo: {type(e).__name__}"})
            return self._json(200, {"page": page})
        finally:
            upload.unlink(missing_ok=True)

    # ------------------------------------------------------------------ body

    def _receive(self, length: int) -> Optional[Path]:
        """Stream the request body to a temporary file; None when it does not arrive whole."""
        try:
            out = tempfile.NamedTemporaryFile(dir=self.server.upload_dir, suffix=".upload", delete=False)
        except OSError:  # the server is closing and its folder is gone
            return None
        path = Path(out.name)
        remaining = length
        try:
            with out:
                while remaining:
                    chunk = self.rfile.read(min(_CHUNK, remaining))
                    if not chunk:
                        break
                    out.write(chunk)
                    remaining -= len(chunk)
        except OSError:  # timeout or reset
            pass
        if remaining:
            path.unlink(missing_ok=True)
            return None
        return path

    def _discard(self, length: int) -> bool:
        """Read a body that will not be used; False when it does not arrive whole."""
        try:
            while length:
                chunk = self.rfile.read(min(_CHUNK, length))
                if not chunk:
                    return False
                length -= len(chunk)
        except OSError:
            return False
        return True

    # ------------------------------------------------------------------ answers

    def _refuse(self, status: int, reason: str) -> None:
        """Answer an upload that is not a photo for a page; info: for the log, not for the terminal."""
        logger.info(f"upload refused ({status}): {reason}")
        self._json(status, {"error": reason})

    def _json(self, status: int, data: dict) -> None:
        self._send(status, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json")

    def _send(self, status: int, body: bytes = b"", content_type: str = "text/plain", cache: str = "no-store") -> None:
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
        except OSError:  # the phone went away
            self.close_connection = True


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):  # a dropped connection is not worth a traceback
        logger.debug(f"request from {client_address[0]} failed", exc_info=True)


class CaptureServer:
    """Serves one capture session. ``session`` may be set after the port is bound, before ``start``."""

    def __init__(self, session: Optional[CaptureSession], host: str, port: int, key: str):
        """
        Raises:
            OSError: The address cannot be listened on (port in use, unknown host)
        """
        self.key = key
        self._httpd = _Server((host, port), _Handler)
        self._httpd.key = key
        self._httpd.session = session
        self._httpd.upload_dir = tempfile.mkdtemp(prefix="basira_capture_")
        self._thread: Optional[threading.Thread] = None

    @property
    def session(self) -> Optional[CaptureSession]:
        return self._httpd.session

    @session.setter
    def session(self, session: CaptureSession) -> None:
        self._httpd.session = session

    @property
    def port(self) -> int:
        return self._httpd.server_address[1]

    def start(self) -> None:
        self._thread = threading.Thread(target=self._httpd.serve_forever, name="basira-capture-http", daemon=True)
        self._thread.start()

    def close(self) -> None:
        if self._thread is not None:
            self._httpd.shutdown()
            self._thread.join()
            self._thread = None
        self._httpd.server_close()
        shutil.rmtree(self._httpd.upload_dir, ignore_errors=True)
