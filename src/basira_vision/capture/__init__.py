"""``basira capture``: read the pages of a paper book from a phone camera.

- session.py: the photos, the reading queue and the page file (no network)
- server.py: the HTTP layer the phone talks to
- page.html: the page the phone opens
"""

from .server import CaptureServer, local_address, new_key
from .session import CaptureError, CaptureSession

__all__ = ["CaptureError", "CaptureServer", "CaptureSession", "local_address", "new_key"]
