"""Real loopback HTTP transfers; no account, browser or remote requests."""

from __future__ import annotations

import http.server
import io
import tempfile
import threading
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from pypdf import PdfWriter

from course_resource_download import download
from course_resource_download.files import claim_or_allocate


class DownloadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        writer = PdfWriter()
        writer.add_blank_page(width=100, height=100)
        buffer = io.BytesIO()
        writer.write(buffer)
        cls.pdf = buffer.getvalue()

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                body = cls.pdf
                if self.path.startswith("/redirect"):
                    self.send_response(302)
                    self.send_header(
                        "Location",
                        "file:///etc/passwd" if self.path == "/redirect-unsafe" else "/valid.pdf",
                    )
                    self.end_headers()
                    return
                if self.path == "/404":
                    self.send_error(404)
                    return
                self.send_response(206 if self.path == "/partial" else 200)
                if self.path == "/text":
                    body = "教材答案\n".encode()
                elif self.path == "/html":
                    body = b"<html>Login required</html>"
                elif self.path == "/empty":
                    body = b""
                if self.path == "/encoded":
                    self.send_header("Content-Encoding", "gzip")
                if self.path != "/no-length":
                    length = len(body) + 100 if self.path == "/truncated" else len(body)
                    self.send_header(
                        "Content-Length",
                        "invalid" if self.path == "/invalid-length" else str(length),
                    )
                if self.path == "/duplicate-length":
                    self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(self.temp.cleanup)
        self.out = Path(self.temp.name)

    def test_valid_small_pdf_text_and_safe_redirect_publish(self):
        for route in ("/valid.pdf", "/redirect", "/no-length"):
            path = self.out / "file.pdf"
            self.assertEqual(download.download_atomic(self.base + route, path), len(self.pdf))
            self.assertEqual(path.read_bytes(), self.pdf)
        path = self.out / "answer.txt"
        self.assertEqual(
            download.download_atomic(self.base + "/text", path), len("教材答案\n".encode())
        )
        self.assertEqual(download.fetch(self.base + "/valid.pdf"), self.pdf)
        self.assertEqual(list(self.out.glob("*.part")), [])

    def test_owned_corrupt_download_is_repaired_without_orphaning(self):
        path = self.out / "notes.pdf"
        path.write_bytes(b"corrupt prior download")
        url = self.base + "/valid.pdf"
        claimed, reused = claim_or_allocate(self.out, "renamed.pdf", url, {url: "notes.pdf"})
        self.assertFalse(reused)
        self.assertEqual(Path(claimed), path)
        with self.assertRaises(ValueError):
            download.download_atomic(self.base + "/html", claimed)
        self.assertEqual(path.read_bytes(), b"corrupt prior download")
        download.download_atomic(url, claimed)
        self.assertEqual(path.read_bytes(), self.pdf)
        self.assertEqual([p.name for p in self.out.iterdir()], ["notes.pdf"])

    def test_bad_transfers_preserve_prior_target_and_clean_part(self):
        path = self.out / "file.pdf"
        path.write_bytes(b"original user document")
        for route in (
            "/html",
            "/truncated",
            "/empty",
            "/404",
            "/partial",
            "/encoded",
            "/invalid-length",
            "/duplicate-length",
            "/redirect-unsafe",
        ):
            with self.subTest(route=route):
                with self.assertRaises((ValueError, urllib.error.URLError)):
                    download.download_atomic(self.base + route, path, timeout=3, chunk=31)
                self.assertEqual(path.read_bytes(), b"original user document")
                self.assertEqual(list(self.out.glob("*.part")), [])

    def test_limits_fail_for_both_declared_and_unframed_data(self):
        path = self.out / "file.pdf"
        for route in ("/valid.pdf", "/no-length"):
            for operation in (
                lambda: download.download_atomic(self.base + route, path, max_bytes=20, chunk=7),
                lambda: download.fetch(self.base + route, max_bytes=20),
            ):
                with self.subTest(route=route):
                    with self.assertRaisesRegex(ValueError, "byte limit"):
                        operation()
                    self.assertFalse(path.exists())
                    self.assertEqual(list(self.out.glob("*.part")), [])

    def test_fetch_never_returns_truncated_prefix(self):
        with self.assertRaisesRegex(ValueError, "Truncated"):
            download.fetch(self.base + "/truncated")

    def test_unsafe_urls_and_invalid_limits_fail_before_network(self):
        for url in (
            "file:///etc/passwd",
            "ftp://example.test/a",
            "https://user:password@example.test/a",
            "javascript:alert(1)",
            "https://example.test/a\r\nInjected:header",
            "http://example.test:99999/a",
            None,
        ):
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    download.download_atomic(url, self.out / "file.pdf")
        for kwargs in (
            {"max_bytes": 0},
            {"max_bytes": -1},
            {"max_bytes": True},
            {"timeout": 0},
            {"timeout": float("nan")},
            {"chunk": 0},
            {"chunk": 99 * 1024 * 1024},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    download.download_atomic(
                        self.base + "/valid.pdf", self.out / "file.pdf", **kwargs
                    )

    def test_fsync_and_replace_failure_keep_previous_target(self):
        path = self.out / "file.pdf"
        path.write_bytes(b"prior bytes")
        for name in ("fsync", "replace"):
            with patch.object(download.os, name, side_effect=OSError("disk unavailable")):
                with self.assertRaises(OSError):
                    download.download_atomic(self.base + "/valid.pdf", path)
            self.assertEqual(path.read_bytes(), b"prior bytes")
            self.assertEqual(list(self.out.glob("*.part")), [])

    def test_unknown_type_is_not_published_even_if_bytes_are_valid(self):
        path = self.out / "file.mkv"
        with self.assertRaisesRegex(ValueError, "Media validation is unsupported"):
            download.download_atomic(self.base + "/valid.pdf", path)
        self.assertFalse(path.exists())

    def test_mp4_extension_does_not_launder_non_mp4_bytes(self):
        path = self.out / "file.mp4"
        with self.assertRaisesRegex(ValueError, "MP4"):
            download.download_atomic(self.base + "/valid.pdf", path)
        self.assertFalse(path.exists())

    def test_logs_never_expose_signed_query_credentials_or_fragment(self):
        secret_url = "https://user:password@example.test/file?token=secret#private"
        for error in [
            ValueError("Failed " + secret_url),
            urllib.error.HTTPError(secret_url, 403, "Forbidden", {}, None),
            urllib.error.URLError(secret_url),
            ValueError("signature=secret token=private"),
        ]:
            text = download.safe_error(error)
            self.assertNotIn("password", text)
            self.assertNotIn("=secret", text)
            self.assertNotIn("=private", text)
            self.assertNotIn("#private", text)


if __name__ == "__main__":
    unittest.main()
