"""icourse163 adapter: tree classification, URL resolution and download paths."""

from __future__ import annotations

import io
import json
import pathlib
import struct
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

import pytest
from pypdf import PdfWriter

from course_resource_download import verify
from course_resource_download.platforms import aicourse as ai
from course_resource_download.platforms import icourse163 as ic


def _box(kind, payload=b""):
    return struct.pack(">I", 8 + len(payload)) + kind + payload


MINIMAL_MP4 = _box(b"ftyp", b"isom" + bytes(4)) + _box(b"moov") + _box(b"mdat", bytes(8))


def chapter(name, lessons, quizs=(), homeworks=()):
    return {
        "name": name,
        "lessons": lessons,
        "quizs": list(quizs),
        "homeworks": list(homeworks),
    }


def lesson(identity, units):
    return {"id": identity, "units": units}


def unit(identity, name, content_type, content_id, duration=None):
    return {
        "id": identity,
        "name": name,
        "contentType": content_type,
        "contentId": content_id,
        "durationInSeconds": duration,
    }


TREE = [
    chapter(
        "第一章 工程材料",
        [
            lesson(
                11,
                [
                    unit(101, "1.1 视频", 1, 9001, 600),
                    unit(102, "1.1 文档", 3, 8001),
                    unit(103, "1.1 检测题", 5, 7001),
                ],
            )
        ],
        quizs=[{"name": "第一章测验"}],
        homeworks=[{"name": "第一章作业"}],
    ),
    chapter("第二章 热处理", [lesson(21, [unit(201, "2.1 视频", 1, 9002, 300)])]),
]


class FakePage:
    """Answers the three platform calls the adapter makes."""

    def __init__(self, tree=None, doc_url=None, doc_type="pdf", vod_quality=(1, 2)):
        self.tree = tree if tree is not None else TREE
        self.doc_url = doc_url
        self.doc_type = doc_type
        self.vod_quality = vod_quality
        self.calls = []
        self.url = "https://www.icourse163.org/learn/TJU-1?tid=2"

    def add_init_script(self, script):
        return None

    def goto(self, url, **kwargs):
        return None

    def close(self):
        return None

    def evaluate(self, script, args):
        kind, params = args
        self.calls.append((kind, params))
        if kind == "tree":
            return json.dumps({"code": 0, "result": {"mocTermDto": {"chapters": self.tree}}})
        if kind == "unit":
            detail = {"type": self.doc_type, "mediaUrl": self.doc_url, "downloadUrl": None}
            return json.dumps({"code": 0, "result": {"detail": detail, "pdfPlayback": None}})
        if kind == "token":
            return json.dumps(
                {"code": 0, "result": {"videoSignDto": {"videoId": 555, "signature": "sig"}}}
            )
        if kind == "vod":
            videos = [
                {"quality": quality, "videoUrl": f"https://cdn.example/{quality}.m3u8"}
                for quality in self.vod_quality
            ]
            return json.dumps({"code": 0, "result": {"videos": videos}})
        raise AssertionError("unexpected call " + kind)


class TestCourseUrl:
    def test_spoc_and_mooc_urls_are_parsed(self):
        assert ic.parse_course_url(
            "https://www.icourse163.org/spoc/learn/TJU-1461178165?tid=1488388446#/learn/content"
        ) == ("TJU-1461178165", "1488388446")
        assert ic.parse_course_url(
            "https://www.icourse163.org/mooc/learn/TJU-1002603039/content?tid=1488430448"
        ) == ("TJU-1002603039", "1488430448")

    def test_foreign_host_is_rejected(self):
        with pytest.raises(ValueError):
            ic.parse_course_url("https://example.com/learn/TJU-1?tid=1")

    def test_missing_term_id_is_rejected(self):
        with pytest.raises(ValueError):
            ic.parse_course_url("https://www.icourse163.org/learn/TJU-1")

    def test_non_learn_path_is_rejected(self):
        with pytest.raises(ValueError):
            ic.parse_course_url("https://www.icourse163.org/course/TJU-1?tid=1")


class TestCollect:
    def test_units_are_split_into_resources_and_records(self):
        resources, records = ic.collect_units(TREE, [])
        urls = [item["url"] for item in resources]
        assert urls == ["videoid:9001", "docid:8001", "videoid:9002"]
        assert resources[0]["extension"] == ".mp4"
        assert resources[1]["extension"] == ".pdf"
        assert resources[0]["duration"] == 600
        reasons = {record["chapter"]: record["reason"] for record in records}
        assert reasons["1.1 检测题"] == "In-chapter check is not downloaded"
        assert "no quiz route is opened" in reasons["第一章测验"]
        assert "no quiz route is opened" in reasons["第一章作业"]
        assert not any(item["url"].startswith("quiz") for item in resources)

    def test_chapter_prefix_filter_excludes_other_chapters(self):
        resources, records = ic.collect_units(TREE, ["第二章"])
        assert [item["url"] for item in resources] == ["videoid:9002"]
        excluded = [
            record for record in records if record["reason"] == "Excluded by --chapter selection"
        ]
        assert [record["chapter"] for record in excluded] == ["第一章 工程材料"]

    def test_unknown_content_types_are_ignored(self):
        tree = [chapter("第一章", [lesson(11, [unit(101, "未知", 9, 1)])])]
        resources, _ = ic.collect_units(tree, [])
        assert resources == []


class TestClassify:
    def test_video_identity_is_classified_from_the_extension_hint(self):
        entry = ai.resource_entry(
            {"name": "1.1 视频", "url": "videoid:9001", "extension": ".mp4"}, include_video=True
        )
        assert entry["status"] == "discovered"
        assert entry["file"] == "1.1 视频.mp4"

    def test_video_is_skipped_without_include_video(self):
        entry = ai.resource_entry({"name": "1.1 视频", "url": "videoid:9001", "extension": ".mp4"})
        assert entry["status"] == "skipped_video"

    def test_document_identity_is_classified(self):
        entry = ai.resource_entry({"name": "1.1 文档", "url": "docid:8001", "extension": ".pdf"})
        assert entry["status"] == "discovered"
        assert entry["file"] == "1.1 文档.pdf"

    def test_section_label_dots_are_not_treated_as_an_extension(self):
        entry = ai.resource_entry({"name": "12.1 螺纹", "url": "docid:8001", "extension": ".pdf"})
        assert entry["file"] == "12.1 螺纹.pdf"

    def test_pseudo_identity_without_hint_is_rejected(self):
        entry = ai.resource_entry({"name": "x", "url": "docid:8001"})
        assert entry["status"] == "failed"

    def test_real_url_still_requires_a_host(self):
        entry = ai.resource_entry({"name": "x", "url": "https:///nohost.pdf"})
        assert entry["status"] == "failed"


class TestResolve:
    def test_document_url_uses_the_media_url_and_type(self):
        page = FakePage(doc_url="https://doc.example/a.pdf", doc_type="pdf")
        url, extension = ic.document_url(page, "TJU-1", "2", {"lessonId": 11, "unitId": 102})
        assert url == "https://doc.example/a.pdf"
        assert extension == ".pdf"

    def test_document_without_media_url_fails(self):
        page = FakePage(doc_url=None)
        with pytest.raises(ValueError):
            ic.document_url(page, "TJU-1", "2", {"lessonId": 11, "unitId": 102})

    def test_document_extension_falls_back_to_the_url_suffix(self):
        page = FakePage(doc_url="https://doc.example/a.pptx", doc_type="unknown")
        _, extension = ic.document_url(page, "TJU-1", "2", {"lessonId": 11, "unitId": 102})
        assert extension == ".pptx"

    def test_video_playlist_picks_the_best_quality(self):
        page = FakePage(vod_quality=(1, 2))
        assert ic.video_playlist(page, 101) == "https://cdn.example/2.m3u8"

    def test_video_without_stream_fails(self):
        page = FakePage(vod_quality=())
        with pytest.raises(ValueError):
            ic.video_playlist(page, 101)


@pytest.fixture
def documents():
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    stream = io.BytesIO()
    writer.write(stream)
    body = stream.getvalue()
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            requests.append(self.path)
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)


class TestDownload:
    def test_document_is_downloaded_and_validated(self, documents, tmp_path):
        base, requests = documents
        page = FakePage(doc_url=base + "/doc.pdf")
        resources, _ = ic.collect_units([TREE[0]], [])
        entries = [ai.resource_entry(item) for item in resources]
        args = SimpleNamespace(out=str(tmp_path), max_video_mb=1024)
        ic.download_entries(page, "TJU-1", "2", entries, args, {})
        doc = next(entry for entry in entries if entry["url"] == "docid:8001")
        assert doc["status"] == "ok"
        assert (tmp_path / "1.1 文档.pdf").exists()
        assert requests == ["/doc.pdf"]

    def test_video_uses_hls_assembly(self, tmp_path):
        page = FakePage()
        resources, _ = ic.collect_units([TREE[0]], [])
        entries = [ai.resource_entry(item, include_video=True) for item in resources]
        args = SimpleNamespace(out=str(tmp_path), max_video_mb=1024)

        def fake_hls(playlist, destination, **kwargs):
            pathlib.Path(destination).write_bytes(MINIMAL_MP4)
            return 1234

        with patch.object(ic, "download_hls", side_effect=fake_hls) as mocked:
            ic.download_entries(page, "TJU-1", "2", entries, args, {})
        video = next(entry for entry in entries if entry["url"] == "videoid:9001")
        assert video["status"] == "ok"
        assert video["size"] == 1234
        assert mocked.call_args[0][0] == "https://cdn.example/2.m3u8"
        assert mocked.call_args.kwargs["max_bytes"] == 1024 * 1024 * 1024

    def test_existing_file_is_reused_without_a_request(self, documents, tmp_path):
        base, requests = documents
        page = FakePage(doc_url=base + "/doc.pdf")
        resources, _ = ic.collect_units([TREE[0]], [])
        entries = [ai.resource_entry(item) for item in resources]
        args = SimpleNamespace(out=str(tmp_path), max_video_mb=1024)
        ic.download_entries(page, "TJU-1", "2", entries, args, {})
        first = len(requests)
        entries = [ai.resource_entry(item) for item in resources]
        ic.download_entries(page, "TJU-1", "2", entries, args, {"docid:8001": "1.1 文档.pdf"})
        doc = next(entry for entry in entries if entry["url"] == "docid:8001")
        assert doc["status"] == "exists"
        assert len(requests) == first

    def test_failure_is_recorded_per_resource(self, tmp_path):
        page = FakePage(doc_url=None)
        resources, _ = ic.collect_units([TREE[0]], [])
        entries = [ai.resource_entry(item) for item in resources]
        args = SimpleNamespace(out=str(tmp_path), max_video_mb=1024)
        ic.download_entries(page, "TJU-1", "2", entries, args, {})
        doc = next(entry for entry in entries if entry["url"] == "docid:8001")
        assert doc["status"] == "failed"
        assert doc["error"]


class TestMain:
    def test_invalid_url_is_a_usage_error(self):
        with pytest.raises(SystemExit):
            ic.main(["--url", "https://example.com/x", "--out", "out"])

    def test_missing_login_state_is_reported(self, tmp_path, capsys):
        code = ic.main(
            [
                "--url",
                "https://www.icourse163.org/learn/TJU-1?tid=2",
                "--out",
                str(tmp_path / "out"),
                "--state",
                str(tmp_path / "missing.json"),
            ]
        )
        assert code == 1
        assert "Login state not found" in capsys.readouterr().out

    def test_non_loopback_cdp_endpoint_is_refused(self, tmp_path):
        with pytest.raises(SystemExit):
            ic.main(
                [
                    "--url",
                    "https://www.icourse163.org/learn/TJU-1?tid=2",
                    "--out",
                    str(tmp_path / "out"),
                    "--cdp-endpoint",
                    "http://10.0.0.1:9222",
                ]
            )

    def test_non_positive_video_limit_is_refused(self, tmp_path):
        with pytest.raises(SystemExit):
            ic.main(
                [
                    "--url",
                    "https://www.icourse163.org/learn/TJU-1?tid=2",
                    "--out",
                    str(tmp_path / "out"),
                    "--max-video-mb",
                    "0",
                ]
            )


class TestTreeFetch:
    def test_empty_tree_is_rejected(self):
        page = Mock()
        page.evaluate.return_value = json.dumps({"code": 0, "result": {"mocTermDto": {}}})
        with pytest.raises(ValueError):
            ic.fetch_tree(page, "2")

    def test_platform_error_is_surfaced(self):
        page = Mock()
        page.evaluate.return_value = json.dumps({"code": 400, "message": "bad term"})
        with pytest.raises(ValueError) as caught:
            ic.fetch_tree(page, "2")
        assert "bad term" in str(caught.value)

    def test_malformed_response_is_rejected(self):
        page = Mock()
        page.evaluate.return_value = "<html>login</html>"
        with pytest.raises(ValueError):
            ic.fetch_tree(page, "2")


def browser_factory(page):
    factory = MagicMock()
    browser = factory.return_value.__enter__.return_value.chromium.launch.return_value
    browser.new_context.return_value.new_page.return_value = page
    return factory, browser


class TestMainFlow:
    def test_discovery_download_and_resume(self, documents, tmp_path, monkeypatch):
        base, requests = documents
        state = tmp_path / "fake-state.json"
        state.write_text("{}", encoding="utf-8")
        out = tmp_path / "out"
        page = FakePage(doc_url=base + "/doc.pdf")
        page.url = "https://www.icourse163.org/learn/TJU-1?tid=2"
        factory, browser = browser_factory(page)
        monkeypatch.setattr(ic, "sync_playwright", factory)
        args = [
            "--url",
            "https://www.icourse163.org/learn/TJU-1?tid=2",
            "--state",
            str(state),
            "--out",
            str(out),
        ]

        assert ic.main(args + ["--list-only"]) == 0
        assert requests == []
        assert (out / "_discovery.json").exists()
        assert not (out / "_manifest.json").exists()

        assert ic.main(args) == 0
        report = json.loads((out / "_manifest.json").read_text(encoding="utf-8"))
        assert report["statistics"]["ok"] == 1
        assert report["statistics"]["skipped_video"] == 2
        assert (out / "1.1 文档.pdf").exists()
        assert requests == ["/doc.pdf"]
        assert verify.main([str(out)]) == 0

        assert ic.main(args) == 0
        resumed = json.loads((out / "_manifest.json").read_text(encoding="utf-8"))
        assert resumed["statistics"]["exists"] == 1
        assert len(requests) == 1
        assert browser.close.call_count == 3

    def test_video_is_downloaded_only_with_the_flag(self, documents, tmp_path, monkeypatch):
        base, requests = documents
        state = tmp_path / "fake-state.json"
        state.write_text("{}", encoding="utf-8")
        out = tmp_path / "out"
        page = FakePage(doc_url=base + "/doc.pdf")
        page.url = "https://www.icourse163.org/learn/TJU-1?tid=2"
        factory, _ = browser_factory(page)
        monkeypatch.setattr(ic, "sync_playwright", factory)

        def fake_hls(playlist, destination, **kwargs):
            pathlib.Path(destination).write_bytes(MINIMAL_MP4)
            return 99

        monkeypatch.setattr(ic, "download_hls", fake_hls)
        args = [
            "--url",
            "https://www.icourse163.org/learn/TJU-1?tid=2",
            "--state",
            str(state),
            "--out",
            str(out),
            "--include-video",
        ]
        assert ic.main(args) == 0
        report = json.loads((out / "_manifest.json").read_text(encoding="utf-8"))
        assert report["statistics"]["ok"] == 3
        assert (out / "1.1 视频.mp4").exists()
        assert report["resources"][0]["url"].startswith("videoid:")

    def test_expired_login_is_reported(self, tmp_path, monkeypatch):
        state = tmp_path / "fake-state.json"
        state.write_text("{}", encoding="utf-8")
        page = FakePage()
        page.url = "https://www.icourse163.org/login"
        factory, _ = browser_factory(page)
        monkeypatch.setattr(ic, "sync_playwright", factory)
        code = ic.main(
            [
                "--url",
                "https://www.icourse163.org/learn/TJU-1?tid=2",
                "--state",
                str(state),
                "--out",
                str(tmp_path / "out"),
            ]
        )
        assert code == 1
        report = json.loads((tmp_path / "out" / "_manifest.json").read_text(encoding="utf-8"))
        assert report["tasks"][-1]["status"] == "failed"
