"""Short-lived, unguessable file URLs for URL-only OCR APIs. Never persist files."""
import contextlib
import mimetypes
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

MAX_BYTES = 128 * 1024 * 1024
TTL = 600


@dataclass
class Asset:
    data: bytes
    mime: str
    expires: float


class AssetStore:
    def __init__(self, max_bytes=MAX_BYTES, ttl=TTL):
        self.items = {}
        self.lock = threading.Lock()
        self.max_bytes, self.ttl = max_bytes, ttl

    def _prune(self):
        now = time.monotonic()
        self.items = {key: item for key, item in self.items.items() if item.expires > now}

    def add(self, files):
        with self.lock:
            self._prune()
            if sum(len(a.data) for a in self.items.values()) + sum(len(data) for data, _ in files) > self.max_bytes:
                raise RuntimeError("المرفقات المؤقتة أكبر من السعة المتاحة. أعد المحاولة لاحقًا.")
            keys = []
            for data, name in files:
                key = secrets.token_urlsafe(32)
                mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
                if not (mime.startswith("image/") or mime in ("application/pdf", "text/plain")):
                    mime = "application/octet-stream"
                self.items[key] = Asset(data, mime, time.monotonic() + self.ttl)
                keys.append(key)
            return keys

    def get(self, key):
        with self.lock:
            self._prune()
            return self.items.get(key)

    def remove(self, keys):
        with self.lock:
            for key in keys:
                self.items.pop(key, None)


STORE = AssetStore()
_SERVER = None
_START_LOCK = threading.Lock()


def handler_for(store):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # File URLs are bearer links; keep them out of access logs.

        def do_HEAD(self):
            self.serve(head=True)

        def do_GET(self):
            self.serve(head=False)

        def serve(self, head):
            path = urlsplit(self.path).path
            if path == "/health":
                data, mime = b'{"status":"ok"}', "application/json"
            else:
                match = re.fullmatch(r"/assets/([A-Za-z0-9_-]{43})", path)
                asset = store.get(match.group(1)) if match else None
                if not asset:
                    self.send_error(404)
                    return
                data, mime = asset.data, asset.mime
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'none'; sandbox")
            self.send_header("Content-Disposition", 'attachment; filename="attachment"')
            self.end_headers()
            if not head:
                try:
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    pass
    return Handler


def public_base_url():
    base = os.getenv("PUBLIC_FILE_BASE_URL", "").strip()
    if not base and os.getenv("RAILWAY_PUBLIC_DOMAIN"):
        base = "https://" + os.environ["RAILWAY_PUBLIC_DOMAIN"]
    parsed = urlsplit(base)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise RuntimeError("روابط مرفقات المعالجة غير معدة. تواصل مع إدارة البوت.")
    return base.rstrip("/")


def start_file_server():
    global _SERVER
    with _START_LOCK:
        if _SERVER is None:
            public_base_url()
            _SERVER = ThreadingHTTPServer(("0.0.0.0", int(os.getenv("PORT", "8080"))), handler_for(STORE))
            _SERVER.daemon_threads = True
            threading.Thread(target=_SERVER.serve_forever, daemon=True, name="temporary-file-links").start()
    return _SERVER


@contextlib.contextmanager
def temporary_links(files):
    base = public_base_url()
    start_file_server()
    keys = STORE.add(list(files))
    try:
        yield [base + "/assets/" + key for key in keys]
    finally:
        STORE.remove(keys)
