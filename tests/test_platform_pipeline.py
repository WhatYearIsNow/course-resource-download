"""Adapter orchestration with simulated browser events and real loopback transfers."""

import io
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import MagicMock, Mock
from urllib.parse import parse_qs, urlsplit

import pytest
from pypdf import PdfWriter

from course_resource_download import verify
from course_resource_download.platforms import aicourse as ai
from course_resource_download.platforms import knowledge as kg
from course_resource_download.platforms import stuonline as stu


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


def browser_factory(page):
    factory = MagicMock()
    browser = factory.return_value.__enter__.return_value.chromium.launch.return_value
    browser.new_context.return_value.new_page.return_value = page
    return factory, browser


def ai_page(base, missing):
    page = Mock()
    emit = None
    page.url = ai.BASE
    tasks = [
        {"documentId": "task", "totalActivities": 2, "teachTaskName": "Main"},
        {"documentId": "locked", "totalActivities": 3, "canClick": False},
        {"documentId": "intro", "totalActivities": 0},
    ]

    def response(marker, payload):
        item = Mock(url=ai.BASE + "/" + marker)
        item.text.return_value = json.dumps({"data": payload})
        emit(item)

    def activity(key):
        resources = [
            {
                "resourcesName": "same.pdf",
                "resourcesDataType": 11,
                "resourcesUrl": base + f"/{key}.pdf",
            }
        ]
        if key == "second":
            resources.extend(
                [
                    {"resourcesName": "duplicate.pdf", "resourcesUrl": base + "/first.pdf"},
                    {"resourcesName": "video", "resourcesDataType": 22},
                    {"resourcesName": "ebook", "resourcesDataType": 21},
                ]
            )
            if missing:
                resources.append({"resourcesName": "broken", "resourcesDataType": 11})
        response(
            "get-project-activity-detail",
            {
                "documentId": "task",
                "taskId": key,
                "activityName": "Same title",
                "detailData": {"knowledgeDetailList": [{"resourcesList": resources}]},
            },
        )

    def register(event, callback):
        nonlocal emit
        assert event == "response"
        emit = callback

    def goto(url, **kwargs):
        page.url = url
        if "singleCourse" in url:
            response("get-project-task-path-cards", {"teachTasks": tasks})
        else:
            assert parse_qs(urlsplit(url).query)["documentId"] == ["task"]
            activity("first")

    page.on.side_effect = register
    page.goto.side_effect = goto
    page.wait_for_timeout.side_effect = lambda _: response("get-course-mapUid", {"mapUid": "map"})
    page.click.side_effect = lambda *args, **kwargs: activity("second")
    page.evaluate.return_value = False
    return page


def knowledge_page(base, missing):
    page = Mock(url=ai.BASE)

    def evaluate(script, params=None):
        if script == kg.INIT_BRIDGE:
            return True
        if script == kg.QUERY_CATALOG:
            return [{"name": "Theme", "nodes": [{"knowledgeId": "node"}]}]
        assert script == kg.QUERY_RESOURCES
        rows = [
            {"name": "same.pdf", "type": 11, "resourceId": key, "url": base + f"/{key}.pdf"}
            for key in ("first", "second")
        ]
        rows.extend(
            [
                {"name": "duplicate.pdf", "url": base + "/first.pdf"},
                {"name": "video", "type": 22, "isVideo": True, "url": base + "/cover.jpg"},
                {"name": "ebook", "type": 21},
            ]
        )
        if missing:
            rows.append({"name": "broken", "type": 11})
        return rows

    page.evaluate.side_effect = evaluate
    return page


@pytest.mark.parametrize("layout", ["tasks", "knowledge"])
@pytest.mark.parametrize("missing", [False, True])
def test_ai_discovery_download_rediscovery_and_resume(
    documents, tmp_path, monkeypatch, missing, layout
):
    base, requests = documents
    state = tmp_path / "fake-state.json"
    state.write_text("{}", encoding="utf-8")
    out = tmp_path / "out"
    page = knowledge_page(base, missing) if layout == "knowledge" else ai_page(base, missing)
    factory, browser = browser_factory(page)
    monkeypatch.setattr(ai, "sync_playwright", factory)
    args = [
        "--course-id",
        "fixture",
        "--class-id",
        "fixture",
        "--state",
        str(state),
        "--out",
        str(out),
        "--layout",
        layout,
    ]
    expected = int(missing)
    assert ai.main(args + ["--list-only"]) == expected
    assert requests == []
    assert ai.main(args) == expected
    report = json.loads((out / "_manifest.json").read_text(encoding="utf-8"))
    assert report["statistics"]["ok"] == 2
    assert report["statistics"].get("failed", 0) == int(missing)
    assert [r["status"] for r in report["tasks"]] == (
        ["ok"] if layout == "knowledge" else ["ok", "locked", "no_activities"]
    )
    assert len(list(out.glob("*.pdf"))) == 2
    assert sorted(requests) == ["/first.pdf", "/second.pdf"]
    before = (out / "_manifest.json").read_bytes()
    assert ai.main(args + ["--list-only"]) == expected
    assert (out / "_manifest.json").read_bytes() == before
    assert ai.main(args) == expected
    assert len(requests) == 2
    resumed = json.loads((out / "_manifest.json").read_text(encoding="utf-8"))
    assert resumed["statistics"]["exists"] == 2
    assert sum(resumed["statistics"].values()) == len(resumed["resources"])
    assert verify.main([str(out)]) == 0
    assert browser.close.call_count == 4


@pytest.mark.parametrize("partial", [False, True])
def test_stu_scope_permissions_and_resume(documents, tmp_path, monkeypatch, partial):
    base, requests = documents
    state = tmp_path / "fake-state.json"
    state.write_text("{}", encoding="utf-8")
    out = tmp_path / "out"
    page = Mock()
    location = []
    metadata = []

    def row(identity, permission="0"):
        return {
            "name": identity + ".pdf",
            "ftype": "pdf",
            "candownload": permission,
            "onclick": f"filePreviewIframe1('preview','name','{identity}')",
        }

    def enter(page, url, path):
        location[:] = path
        return True

    def evaluate(script, args=None):
        if script == stu.JS_ROWS:
            if not location:
                return [{"fid": "selected", "name": "Selected"}, {"fid": "other", "name": "Other"}]
            assert location == ["selected"]
            return [row("allowed")] + ([row("denied", "1")] if partial else [])
        assert script == stu.JS_DOWNLOAD
        metadata.append(args[0])
        assert args[0] == "allowed"
        return json.dumps({"dataUrl": base + "/allowed.pdf"})

    page.evaluate.side_effect = evaluate
    factory, browser = browser_factory(page)
    monkeypatch.setattr(stu, "sync_playwright", factory)
    monkeypatch.setattr(stu, "open_folder", enter)
    args = [
        "--url",
        "https://stuonline.zhihuishu.com/fixture",
        "--state",
        str(state),
        "--out",
        str(out),
        "--folder",
        "Selected",
    ]
    if partial:
        args.extend(["--folder", "Missing"])
    assert stu.main(args + ["--list-only"]) == int(partial)
    assert requests == metadata == []
    assert stu.main(args) == int(partial)
    report = json.loads((out / "_manifest.json").read_text(encoding="utf-8"))
    assert report["statistics"]["ok"] == 1
    assert "Other" not in [f["folder"] for f in report["folders"]]
    if partial:
        assert report["statistics"]["permission_denied"] == 1
        assert any(f["folder"] == "Missing" and f["status"] == "failed" for f in report["folders"])
    before = (out / "_manifest.json").read_bytes()
    assert stu.main(args + ["--list-only"]) == int(partial)
    assert (out / "_manifest.json").read_bytes() == before
    assert stu.main(args) == int(partial)
    assert metadata == ["allowed", "allowed"]
    assert requests == ["/allowed.pdf"]
    assert verify.main([str(out)]) == 0
    assert browser.close.call_count == 4
