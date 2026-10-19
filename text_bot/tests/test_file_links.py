import http.client
import threading
import unittest
from unittest.mock import patch
from http.server import ThreadingHTTPServer

from utils.file_links import AssetStore, handler_for, temporary_links


class FileLinkTests(unittest.TestCase):
    def test_links_expire_and_capacity_is_enforced(self):
        store = AssetStore(max_bytes=5, ttl=10)
        with patch("utils.file_links.time.monotonic", return_value=100):
            key = store.add([(b"hello", "one.txt")])[0]
            with self.assertRaises(RuntimeError):
                store.add([(b"x", "two.txt")])
        with patch("utils.file_links.time.monotonic", return_value=111):
            self.assertIsNone(store.get(key))
            self.assertEqual(len(store.add([(b"again", "three.txt")])), 1)

    def test_lease_is_revoked_on_success_and_exception(self):
        store = AssetStore()
        with patch("utils.file_links.STORE", store), patch("utils.file_links.start_file_server"), \
                patch("utils.file_links.public_base_url", return_value="https://files.test"):
            with temporary_links([(b"private", "image.png")]) as urls:
                key = urls[0].split("/")[-1]
                self.assertIsNotNone(store.get(key))
            self.assertIsNone(store.get(key))
            with self.assertRaises(ValueError):
                with temporary_links([(b"private", "image.png")]):
                    raise ValueError("request failed")
            self.assertEqual(store.items, {})

    def test_http_can_fetch_only_current_leased_bytes_and_head_matches(self):
        store = AssetStore()
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(store))
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        def request(path, method="GET"):
            client = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
            try:
                client.request(method, path)
                r = client.getresponse()
                return r.status, dict(r.getheaders()), r.read()
            finally:
                client.close()
        try:
            key = store.add([(b"exact image bytes", "test.png")])[0]
            status, headers, body = request("/assets/" + key)
            self.assertEqual((status, body), (200, b"exact image bytes"))
            self.assertEqual(headers["Cache-Control"], "no-store")
            self.assertEqual(headers["Content-Type"], "image/png")
            self.assertEqual(request("/assets/" + key, "HEAD")[2], b"")
            store.remove([key])
            self.assertEqual(request("/assets/" + key)[0], 404)
            self.assertEqual(request("/../bot.py")[0], 404)
            self.assertEqual(request("/health")[0], 200)
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=3)
