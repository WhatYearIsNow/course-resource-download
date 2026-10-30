"""Offline Chaoxing adapter contracts: no live service, credential or browser access."""

import json
import struct
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

import pytest

from course_resource_download.platforms import chaoxing as cx

COURSE_URL = (
    "https://mooc2-ans.chaoxing.com/mooc2-ans/mycourse/stu"
    "?courseid=111111111&clazzid=222222222&cpi=333333333&enc=" + "c" * 32 + "&t=1700000000000"
)
HEX1 = "a" * 32
HEX2 = "b" * 32
ENC = "d" * 32
SOURCE = f'var mooc1Domain = ServerHost.mooc1Domain; var enc = "{ENC}"; var cpi = 333333333;'

CARDS_ONE = f'<div>{{"objectid":"{HEX1.upper()}","objectId":"{HEX1}"}}</div>'
CARDS_TWO = f'<script>var data = {{"objectid":"{HEX2}"}};</script>{{"objectid":"{HEX1}"}}'


def status_body(**overrides):
    body = {
        "objectid": HEX1,
        "filename": "slides.pptx",
        "download": "http://d0.cldisk.com/download/" + HEX1,
        "length": 10,
    }
    body.update(overrides)
    return body


def chapter(label, knowledge_id, clickable=True):
    onclick = f"toOld('111111111', '{knowledge_id}', '222222222',0)"
    return {
        "title": label,
        "clickable": clickable,
        "onclick": onclick if clickable else None,
        "label": label,
    }


class FakePage:
    def __init__(self, bodies, expire_on=None, passport="https://passport2.chaoxing.com/login"):
        self.bodies = bodies
        self.expire_on = expire_on
        self.passport = passport
        self.url = cx.BASE_MOOC1 + "/mycourse/studentstudy"
        self.gotos = 0

    def goto(self, url, **kwargs):
        self.gotos += 1
        self.url = self.passport if self.gotos == self.expire_on else url

    def evaluate(self, script, *args):
        assert script == cx.JS_FETCH
        url = args[0]
        for key, text in self.bodies.items():
            if key in url:
                return text
        raise AssertionError("unexpected fetch: " + url)


@pytest.fixture
def text_server():
    body = b"hello course\n"
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            seen.append({"path": self.path, "referer": self.headers.get("Referer")})
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", seen
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)


@pytest.fixture
def mp4_server():
    """Serve a structurally valid MP4 so the container check can pass."""
    body = (
        struct.pack(">I", 16)
        + b"ftypisom"
        + bytes(4)
        + struct.pack(">I", 8)
        + b"moov"
        + struct.pack(">I", 16)
        + b"mdat"
        + bytes(8)
    )
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            seen.append({"path": self.path, "referer": self.headers.get("Referer")})
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", seen
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)


class TestClassification:
    def test_document_is_discovered_with_safe_name(self):
        entry = cx.classify_status(status_body(filename="a/b:c.pptx"))
        assert entry["status"] == "discovered"
        assert entry["file"] == "a_b_c.pptx"
        assert entry["url"] == "objectid:" + HEX1

    @pytest.mark.parametrize(
        "filename, include, expected",
        [
            ("video.mp4", False, "skipped_video"),
            ("video.mp4", True, "discovered"),
            ("video.m4v", True, "discovered"),
            ("video.mkv", True, "skipped_unsupported_format"),
            ("old.ppt", False, "skipped_unsupported_format"),
            ("archive.zip", False, "skipped_non_document"),
        ],
    )
    def test_excluded_types(self, filename, include, expected):
        entry = cx.classify_status(status_body(filename=filename), include_video=include)
        assert entry["status"] == expected

    def test_video_keeps_its_own_name_and_identity(self):
        entry = cx.classify_status(
            status_body(filename="How to Write a Killer resume.mp4"), include_video=True
        )
        assert entry["status"] == "discovered"
        assert entry["file"] == "How to Write a Killer resume.mp4"
        assert entry["url"] == "objectid:" + HEX1

    def test_missing_download_is_permission_denied(self):
        assert cx.classify_status(status_body(download=None))["status"] == "permission_denied"

    @pytest.mark.parametrize(
        "body",
        [None, {}, status_body(filename=None), {"objectid": "  "}],
    )
    def test_malformed_status_fails(self, body):
        assert cx.classify_status(body)["status"] == "failed"


class TestParsing:
    @pytest.mark.parametrize(
        "onclick, expected",
        [
            (
                "toOld('111111111', '123456789', '222222222',0)",
                ("111111111", "123456789", "222222222"),
            ),
            (
                "return toOld('111111111','123456789','222222222',0)",
                ("111111111", "123456789", "222222222"),
            ),
            ("toOld('abc', '123456789', '222222222',0)", None),
            ("openFolder(this)", None),
            (None, None),
        ],
    )
    def test_knowledge_id(self, onclick, expected):
        assert cx.parse_knowledge_id(onclick) == expected

    def test_toold_source(self):
        assert cx.parse_toold_source(SOURCE) == (ENC, "333333333")
        assert cx.parse_toold_source("no values here") == (None, None)
        assert cx.parse_toold_source(None) == (None, None)

    def test_object_ids_are_deduplicated_and_lowercased(self):
        assert cx.extract_object_ids(CARDS_ONE) == [HEX1]
        assert cx.extract_object_ids(CARDS_TWO) == [HEX2, HEX1]
        assert cx.extract_object_ids("objectid:" + "e" * 31) == []
        assert cx.extract_object_ids(None) == []

    def test_chapter_selection_is_case_insensitive_prefix(self):
        assert cx.selected_chapter("2.1 Introduction", ["2."])
        assert cx.selected_chapter("2.1 Introduction", ["2.1 intro"])
        assert not cx.selected_chapter("1.2 Teaching Schedule", ["2."])
        assert cx.selected_chapter("anything", [])

    def test_collect_chapters_reports_locked_items(self):
        raw = {
            "items": [chapter("2.1 Intro", "123456789"), chapter("Locked", "999", False), "junk"]
        }
        plan, locked = cx.collect_chapters(raw, cx.BASE_MOOC1, "333333333", ENC)
        assert [item["knowledgeId"] for item in plan] == ["123456789"]
        assert locked == ["Locked", "Unnamed chapter"]
        assert "enc=" + ENC in plan[0]["study"]
        assert "cpi=333333333" in plan[0]["cards"]
        with pytest.raises(ValueError):
            cx.collect_chapters({"items": []}, cx.BASE_MOOC1, ENC, "333333333")
        with pytest.raises(ValueError):
            cx.collect_chapters(
                {"items": [chapter("Locked", "999", False)]}, cx.BASE_MOOC1, ENC, "1"
            )


class TestTraversal:
    def bodies(self):
        doc = status_body()
        video = status_body(objectid=HEX2, filename="talk.mp4")
        return (
            {
                "knowledgeid=123456789": CARDS_ONE,
                "knowledgeid=987654321": CARDS_TWO,
                "ananas/status/" + HEX1: json.dumps(doc),
                "ananas/status/" + HEX2: json.dumps(video),
            },
            doc,
        )

    def make_plan(self, labels=("2.1 Intro", "2.3 Content")):
        return [
            {
                "label": label,
                "knowledgeId": knowledge_id,
                "study": "s",
                "cards": cx.cards_url(
                    cx.BASE_MOOC1, "111111111", "222222222", "333333333", knowledge_id
                ),
            }
            for label, knowledge_id in zip(labels, ("123456789", "987654321"))
        ]

    def test_visit_records_all_objects_and_merges_chapters(self):
        bodies, doc = self.bodies()
        page = FakePage(bodies)
        plan = self.make_plan()
        entries, store, signed, records, checkpoints = [], {}, {}, [], []
        cx.visit_chapters(
            page,
            plan,
            cx.BASE_MOOC1,
            SimpleNamespace(include_video=False),
            entries,
            store,
            signed,
            records,
            lambda: checkpoints.append(1),
        )
        assert [record["status"] for record in records] == ["ok", "ok"]
        assert [record["resourceCount"] for record in records] == [1, 2]
        assert [entry["status"] for entry in entries] == ["discovered", "skipped_video"]
        assert entries[0]["chapters"] == ["2.1 Intro", "2.3 Content"]
        assert len(signed) == 1
        assert page.gotos == 2
        assert len(checkpoints) >= 3

    def test_status_payload_drives_classification(self):
        bodies, doc = self.bodies()
        page = FakePage(bodies)
        plan = [
            {
                "label": "2.1 Intro",
                "knowledgeId": "123456789",
                "study": "s",
                "cards": cx.cards_url(
                    cx.BASE_MOOC1, "111111111", "222222222", "333333333", "123456789"
                ),
            }
        ]
        entries, store, signed, records = [], {}, {}, []
        cx.visit_chapters(
            page,
            plan,
            cx.BASE_MOOC1,
            SimpleNamespace(include_video=False),
            entries,
            store,
            signed,
            records,
        )
        assert entries[0]["objectid"] == HEX1
        assert signed["objectid:" + HEX1] == doc["download"]

    def test_include_video_reaches_classification(self):
        bodies, _ = self.bodies()
        page = FakePage(bodies)
        plan = self.make_plan()
        entries, store, signed, records = [], {}, {}, []
        cx.visit_chapters(
            page,
            plan,
            cx.BASE_MOOC1,
            SimpleNamespace(include_video=True),
            entries,
            store,
            signed,
            records,
        )
        assert [entry["status"] for entry in entries] == ["discovered", "discovered"]
        assert "objectid:" + HEX2 in signed

    def test_login_expiry_fails_node_and_continues(self):
        bodies, _ = self.bodies()
        page = FakePage(bodies, expire_on=1)
        plan = self.make_plan(labels=("First", "Second"))
        entries, store, signed, records = [], {}, {}, []
        cx.visit_chapters(
            page,
            plan,
            cx.BASE_MOOC1,
            SimpleNamespace(include_video=False),
            entries,
            store,
            signed,
            records,
        )
        assert records[0]["status"] == "failed"
        assert "expired" in records[0]["reason"].lower()
        assert records[1]["status"] == "ok"
        assert [entry["status"] for entry in entries] == ["skipped_video", "discovered"]

    def test_malformed_status_json_is_recorded_as_failure(self):
        page = FakePage(
            {
                "knowledgeid=123456789": CARDS_ONE,
                "ananas/status/" + HEX1: "<html>not json</html>",
            }
        )
        plan = [
            {
                "label": "2.1 Intro",
                "knowledgeId": "123456789",
                "study": "s",
                "cards": cx.cards_url(
                    cx.BASE_MOOC1, "111111111", "222222222", "333333333", "123456789"
                ),
            }
        ]
        entries, store, signed, records = [], {}, {}, []
        cx.visit_chapters(
            page,
            plan,
            cx.BASE_MOOC1,
            SimpleNamespace(include_video=False),
            entries,
            store,
            signed,
            records,
        )
        assert records[0]["status"] == "ok"
        assert entries[0]["status"] == "failed"
        assert entries[0]["objectid"] == HEX1


class TestDownload:
    def entry(self, base):
        body = status_body(filename="notes.txt", download=base + "/file.txt")
        entry = cx.classify_status(body)
        return entry, {"objectid:" + HEX1: body["download"]}

    def args(self, out):
        return SimpleNamespace(out=str(out), include_video=False, list_only=False)

    def test_download_sends_chaoxing_referer(self, tmp_path, text_server):
        base, seen = text_server
        entry, signed = self.entry(base)
        index = {}
        checkpoints = []
        cx.download_entries(
            [entry], signed, self.args(tmp_path), index, lambda: checkpoints.append(1)
        )
        assert entry["status"] == "ok"
        assert entry["size"] == len(b"hello course\n")
        assert index[entry["url"]] == entry["file"]
        assert (tmp_path / entry["file"]).read_bytes() == b"hello course\n"
        assert seen[0]["referer"] == cx.REFERER
        assert checkpoints

    def test_video_is_downloaded_with_the_chaoxing_referer(self, tmp_path, mp4_server):
        base, seen = mp4_server
        body = status_body(filename="How to Write a Killer resume.mp4", download=base + "/v.mp4")
        entry = cx.classify_status(body, include_video=True)
        assert entry["status"] == "discovered"
        args = SimpleNamespace(out=str(tmp_path), include_video=True, list_only=False)
        cx.download_entries([entry], {entry["url"]: body["download"]}, args, {})
        assert entry["status"] == "ok"
        assert (tmp_path / "How to Write a Killer resume.mp4").exists()
        assert seen[0]["referer"] == cx.REFERER

    def test_video_without_the_flag_is_never_requested(self, tmp_path, mp4_server):
        base, seen = mp4_server
        body = status_body(filename="talk.mp4", download=base + "/v.mp4")
        entry = cx.classify_status(body, include_video=False)
        args = SimpleNamespace(out=str(tmp_path), include_video=False, list_only=False)
        cx.download_entries([entry], {entry["url"]: body["download"]}, args, {})
        assert entry["status"] == "skipped_video"
        assert seen == []

    def test_repeated_run_reuses_verified_file(self, tmp_path, text_server):
        base, seen = text_server
        entry, signed = self.entry(base)
        index = {}
        cx.download_entries([entry], signed, self.args(tmp_path), index)
        entry2, signed2 = self.entry(base)
        cx.download_entries([entry2], signed2, self.args(tmp_path), index)
        assert entry2["status"] == "exists"
        assert len(seen) == 1

    def test_missing_signed_url_fails_without_download(self, tmp_path, text_server):
        base, seen = text_server
        entry, _ = self.entry(base)
        cx.download_entries([entry], {}, self.args(tmp_path), {})
        assert entry["status"] == "failed"
        assert "Signed download URL" in entry["error"]
        assert seen == []

    def test_server_error_keeps_no_final_file(self, tmp_path):
        import contextlib

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.send_response(500)
                self.send_header("Content-Length", "0")
                self.end_headers()

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            entry = cx.classify_status(
                status_body(
                    filename="notes.txt", download=f"http://127.0.0.1:{server.server_port}/x.txt"
                )
            )
            signed = {"objectid:" + HEX1: f"http://127.0.0.1:{server.server_port}/x.txt"}
            with contextlib.suppress(Exception):
                cx.download_entries([entry], signed, self.args(tmp_path), {})
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=5)
        assert entry["status"] == "failed"
        assert not [p for p in tmp_path.iterdir() if not p.name.endswith(".lock")]


def browser_factory(page):
    factory = MagicMock()
    browser = factory.return_value.__enter__.return_value.chromium.launch.return_value
    browser.new_context.return_value.new_page.return_value = page
    return factory


def orchestration_page(catalog, statuses):
    page = Mock()
    page.url = COURSE_URL

    def goto(url, **kwargs):
        page.url = url

    def evaluate(script, *args):
        if script == cx.JS_FRAME_SRC:
            return "https://mooc2-ans.chaoxing.com/mooc2-ans/mycourse/studentcourse?x=1"
        if script == cx.JS_CHAPTERS:
            return json.dumps(
                {
                    "items": [chapter("2.1 Intro", "123456789")],
                    "source": SOURCE,
                    "mooc1": cx.BASE_MOOC1,
                }
            )
        assert script == cx.JS_FETCH
        url = args[0]
        if "knowledge/cards" in url:
            return catalog
        for key, text in statuses.items():
            if key in url:
                return text
        raise AssertionError("unexpected fetch: " + url)

    page.goto.side_effect = goto
    page.evaluate.side_effect = evaluate
    return page


class TestMain:
    def args(self, out, *extra):
        return [
            "--url",
            COURSE_URL,
            "--out",
            str(out),
            "--state",
            str(out / "state.json"),
            *extra,
        ]

    def test_download_run_writes_manifest_and_file(self, tmp_path, text_server):
        base, seen = text_server
        out = tmp_path / "out"
        out.mkdir()
        (out / "state.json").write_text("{}", encoding="utf-8")
        page = orchestration_page(
            CARDS_ONE,
            {
                "ananas/status/" + HEX1: json.dumps(
                    status_body(filename="notes.txt", download=base + "/f.txt")
                )
            },
        )
        with patch.object(cx, "sync_playwright", browser_factory(page)):
            assert cx.main(self.args(out)) == 0
        manifest = json.loads((out / "_manifest.json").read_text(encoding="utf-8"))
        assert manifest["resources"][0]["status"] == "ok"
        assert manifest["resources"][0]["url"] == "objectid:" + HEX1
        payload = json.dumps(manifest)
        assert "cldisk" not in payload and "at_=" not in payload
        assert len(seen) == 1
        page.close.assert_not_called()

    def test_list_only_writes_discovery_without_downloads(self, tmp_path, text_server):
        base, seen = text_server
        out = tmp_path / "out"
        out.mkdir()
        (out / "state.json").write_text("{}", encoding="utf-8")
        page = orchestration_page(
            CARDS_ONE,
            {
                "ananas/status/" + HEX1: json.dumps(
                    status_body(filename="notes.txt", download=base + "/f.txt")
                )
            },
        )
        with patch.object(cx, "sync_playwright", browser_factory(page)):
            assert cx.main(self.args(out, "--list-only")) == 0
        assert (out / "_discovery.json").exists()
        assert not (out / "_manifest.json").exists()
        assert seen == []

    def test_chapter_selection_limits_traversal(self, tmp_path, text_server):
        base, seen = text_server
        out = tmp_path / "out"
        out.mkdir()
        (out / "state.json").write_text("{}", encoding="utf-8")
        page = Mock()
        page.url = COURSE_URL
        page.goto.side_effect = lambda url, **kwargs: setattr(page, "url", url)
        catalog_two = json.dumps(
            {
                "items": [chapter("1.1 Other", "888888888"), chapter("2.1 Intro", "123456789")],
                "source": SOURCE,
                "mooc1": cx.BASE_MOOC1,
            }
        )

        def evaluate(script, *args):
            if script == cx.JS_FRAME_SRC:
                return "https://mooc2-ans.chaoxing.com/mooc2-ans/mycourse/studentcourse?x=1"
            if script == cx.JS_CHAPTERS:
                return catalog_two
            return ""

        page.evaluate.side_effect = evaluate
        with patch.object(cx, "sync_playwright", browser_factory(page)):
            assert cx.main(self.args(out, "--list-only", "--chapter", "2.")) == 0
        discovery = json.loads((out / "_discovery.json").read_text(encoding="utf-8"))
        chapters = [record["chapter"] for record in discovery["tasks"]]
        assert "1.1 Other" in chapters
        assert discovery["tasks"][chapters.index("1.1 Other")]["reason"].startswith("Excluded")

    def test_login_expiry_fails_the_run(self, tmp_path, text_server):
        base, seen = text_server
        out = tmp_path / "out"
        out.mkdir()
        (out / "state.json").write_text("{}", encoding="utf-8")
        page = orchestration_page(CARDS_ONE, {})
        page.goto.side_effect = lambda url, **kwargs: setattr(
            page, "url", "https://passport2.chaoxing.com/login?refer=x"
        )
        with patch.object(cx, "sync_playwright", browser_factory(page)):
            assert cx.main(self.args(out)) == 1
        manifest = json.loads((out / "_manifest.json").read_text(encoding="utf-8"))
        assert manifest["tasks"][0]["status"] == "failed"

    def test_missing_state_file_is_rejected(self, tmp_path):
        assert cx.main(self.args(tmp_path)) == 1

    def test_invalid_host_is_rejected(self, tmp_path):
        with pytest.raises(SystemExit):
            cx.main(["--url", "https://example.com/course", "--out", str(tmp_path)])

    def test_missing_course_identity_is_rejected(self, tmp_path, capsys):
        with pytest.raises(SystemExit):
            cx.main(["--url", "https://mooc2-ans.chaoxing.com/stu?enc=x", "--out", str(tmp_path)])
        assert "courseid" in capsys.readouterr().err
