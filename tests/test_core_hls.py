"""HLS assembly: playlist parsing, segment concatenation and remux publication."""

from __future__ import annotations

import struct
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from course_resource_download import hls


def box(kind, payload=b""):
    return struct.pack(">I", 8 + len(payload)) + kind + payload


def minimal_mp4():
    """A structurally valid ISO base media file: ftyp, moov, mdat."""
    return box(b"ftyp", b"isom" + b"\x00" * 4) + box(b"moov") + box(b"mdat", b"\x00" * 8)


MASTER = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=400000,RESOLUTION=640x360
low/index.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=1200000,RESOLUTION=1280x720
high/index.m3u8
"""

MEDIA = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-MAP:URI="init.mp4"
#EXTINF:4.0,
seg0.ts
#EXTINF:4.0,
seg1.ts
#EXT-X-ENDLIST
"""


class PlaylistTests(unittest.TestCase):
    def test_master_playlist_picks_highest_bandwidth(self):
        self.assertEqual(
            hls.parse_master_playlist(MASTER, "https://cdn.example/a/master.m3u8"),
            "https://cdn.example/a/high/index.m3u8",
        )

    def test_master_playlist_without_variants_returns_none(self):
        self.assertIsNone(hls.parse_master_playlist(MEDIA, "https://cdn.example/a/index.m3u8"))

    def test_media_playlist_resolves_relative_segments_and_map(self):
        segments, init = hls.parse_media_playlist(MEDIA, "https://cdn.example/a/high/index.m3u8")
        self.assertEqual(
            segments,
            ["https://cdn.example/a/high/seg0.ts", "https://cdn.example/a/high/seg1.ts"],
        )
        self.assertEqual(init, "https://cdn.example/a/high/init.mp4")

    def test_media_playlist_keeps_absolute_segments(self):
        text = "#EXTM3U\n#EXTINF:1,\nhttps://other.example/x.ts\n"
        segments, init = hls.parse_media_playlist(text, "https://cdn.example/a/index.m3u8")
        self.assertEqual(segments, ["https://other.example/x.ts"])
        self.assertIsNone(init)


class FakeFfmpeg:
    """Stand-in for the binary: writes a valid MP4 where ffmpeg would."""

    def __init__(self, fail=False):
        self.commands = []
        self.fail = fail

    def __call__(self, command, **kwargs):
        self.commands.append(command)
        result = type("Result", (), {"returncode": 1 if self.fail else 0, "stderr": "boom"})()
        if not self.fail:
            Path(command[-1]).write_bytes(minimal_mp4())
        return result


class DownloadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.segments = [b"segment-zero", b"segment-one"]

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                if self.path.endswith("/master.m3u8"):
                    body, kind = MASTER.encode(), "application/vnd.apple.mpegurl"
                elif self.path.endswith("/high/index.m3u8"):
                    body, kind = MEDIA.encode(), "application/vnd.apple.mpegurl"
                elif self.path.endswith("seg0.ts"):
                    body, kind = cls.segments[0], "video/mp2t"
                elif self.path.endswith("seg1.ts"):
                    body, kind = cls.segments[1], "video/mp2t"
                elif self.path.endswith("init.mp4"):
                    body, kind = b"init", "video/mp4"
                else:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.worker = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.worker.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.worker.join(timeout=5)

    def test_download_hls_assembles_segments_and_publishes_validated_mp4(self):
        fake = FakeFfmpeg()
        with TemporaryDirectory() as directory:
            dest = Path(directory) / "video.mp4"
            with patch.object(hls.subprocess, "run", fake):
                total = hls.download_hls(
                    self.base + "/master.m3u8", dest, ffmpeg="ffmpeg", timeout=10
                )
            self.assertEqual(total, sum(len(part) for part in self.segments) + len(b"init"))
            self.assertEqual(dest.read_bytes(), minimal_mp4())
            self.assertEqual(list(Path(directory).glob(".crd-download-*")), [])
            command = fake.commands[0]
            self.assertEqual(command[0], "ffmpeg")
            self.assertIn("-c", command)
            self.assertEqual(command[command.index("-c") + 1], "copy")

    def test_missing_ffmpeg_is_a_reported_failure(self):
        with TemporaryDirectory() as directory:
            dest = Path(directory) / "video.mp4"
            with patch.object(hls.shutil, "which", return_value=None):
                with patch.dict(hls.os.environ, {}, clear=True):
                    with self.assertRaises(RuntimeError) as caught:
                        hls.download_hls(self.base + "/master.m3u8", dest, timeout=10)
            self.assertIn("ffmpeg", str(caught.exception))
            self.assertFalse(dest.exists())

    def test_configured_ffmpeg_must_exist(self):
        with patch.dict(hls.os.environ, {"CRD_FFMPEG": "definitely-not-a-file"}):
            with self.assertRaises(RuntimeError):
                hls.find_ffmpeg()

    def test_ffmpeg_failure_preserves_existing_destination(self):
        with TemporaryDirectory() as directory:
            dest = Path(directory) / "video.mp4"
            dest.write_bytes(b"previous")
            with patch.object(hls.subprocess, "run", FakeFfmpeg(fail=True)):
                with self.assertRaises(RuntimeError):
                    hls.download_hls(self.base + "/master.m3u8", dest, ffmpeg="ffmpeg", timeout=10)
            self.assertEqual(dest.read_bytes(), b"previous")
            self.assertEqual(list(Path(directory).glob(".crd-download-*")), [])

    def test_byte_limit_is_enforced_across_segments(self):
        with TemporaryDirectory() as directory:
            dest = Path(directory) / "video.mp4"
            with patch.object(hls.subprocess, "run", FakeFfmpeg()):
                with self.assertRaises(ValueError):
                    hls.download_hls(
                        self.base + "/master.m3u8",
                        dest,
                        ffmpeg="ffmpeg",
                        timeout=10,
                        max_bytes=4,
                    )
            self.assertFalse(dest.exists())

    def test_invalid_byte_limit_is_rejected(self):
        with TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                hls.download_hls(
                    self.base + "/master.m3u8",
                    Path(directory) / "video.mp4",
                    ffmpeg="ffmpeg",
                    max_bytes=0,
                )

    def test_playlist_without_segments_is_rejected(self):
        with TemporaryDirectory() as directory:
            dest = Path(directory) / "video.mp4"
            with patch.object(hls, "fetch", return_value=b"#EXTM3U\n#EXT-X-ENDLIST\n"):
                with self.assertRaises(ValueError):
                    hls.download_hls("https://cdn.example/x.m3u8", dest, ffmpeg="ffmpeg")

    def test_non_http_segment_is_rejected(self):
        playlist = b"#EXTM3U\n#EXTINF:1,\nfile:///etc/passwd\n"
        with TemporaryDirectory() as directory:
            with patch.object(hls, "fetch", return_value=playlist):
                with self.assertRaises(ValueError):
                    hls.download_hls(
                        "https://cdn.example/x.m3u8", Path(directory) / "v.mp4", ffmpeg="ffmpeg"
                    )


class RealFfmpegTests(unittest.TestCase):
    """Runs the real binary when present; CI without ffmpeg skips honestly."""

    def setUp(self):
        try:
            self.binary = hls.find_ffmpeg()
        except RuntimeError:
            self.skipTest("ffmpeg is not installed on this machine")

    def test_real_ffmpeg_remuxes_a_transport_stream(self):
        import subprocess

        with TemporaryDirectory() as directory:
            source = Path(directory) / "source.ts"
            built = subprocess.run(
                [
                    self.binary,
                    "-y",
                    "-loglevel",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "testsrc=size=160x120:rate=10:duration=1",
                    "-c:v",
                    "libx264",
                    "-pix_fmt",
                    "yuv420p",
                    "-f",
                    "mpegts",
                    str(source),
                ],
                capture_output=True,
                text=True,
            )
            if built.returncode != 0:
                self.skipTest("ffmpeg build lacks libx264")
            output = Path(directory) / "out.mp4"
            hls._remux(self.binary, str(source), str(output))
            self.assertTrue(output.exists())
            from course_resource_download.verify import check_file

            ok, reason, _ = check_file(output, extension=".mp4")
            self.assertTrue(ok, reason)


if __name__ == "__main__":
    unittest.main()
